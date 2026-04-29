#!/usr/bin/env python3
"""
GygesLink — API REST pour l'app desktop

Sert les endpoints /api/* pour la gestion à distance du boîtier.
Écoute sur 192.168.100.1:4430 (HTTP, lien USB uniquement).
Le portail setup reste sur :443 (HTTPS).

Fonctionne EN PARALLÈLE du portail setup :
  - Si setup-done absent : portail ET api actifs
  - Si setup-done présent : seul l'api est actif (tor+iptables opérationnels)
"""

import json
import logging
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

DATA_DIR = Path("/data/gygeslink")
SETUP_DONE_FILE = DATA_DIR / "setup-done"
WG_CONF_FILE = DATA_DIR / "wg0.conf"
WG_EXPIRY_FILE = DATA_DIR / "wg-expiry.txt"
WIFI_CONF_FILE = DATA_DIR / "wifi.conf"
BRIDGES_CONF_FILE = DATA_DIR / "bridges.conf"

API_HOST = "192.168.100.1"
API_PORT = 4430

TOR_CONTROL_HOST = "127.0.0.1"
TOR_CONTROL_PORT = 9051
TOR_COOKIE_PATH = "/var/run/tor/control.authcookie"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [gygeslink-api] %(levelname)s %(message)s",
)
logger = logging.getLogger("gygeslink-api")

app = Flask(__name__)
app.secret_key = os.urandom(32)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day"],
    storage_uri="memory://",
)


def _json_ok(**kwargs) -> tuple:
    return jsonify({"success": True, **kwargs}), 200


def _json_error(error: str, message: str, status: int = 400) -> tuple:
    return jsonify({"success": False, "error": error, "message": message}), status


def _tor_cookie_auth() -> str:
    if not os.path.exists(TOR_COOKIE_PATH):
        return ""
    with open(TOR_COOKIE_PATH, "rb") as f:
        cookie = f.read()
    return cookie.hex()


def _tor_command(cmd: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((TOR_CONTROL_HOST, TOR_CONTROL_PORT))
        cookie_hex = _tor_cookie_auth()
        if cookie_hex:
            s.sendall(f'AUTHENTICATE "{cookie_hex}"\r\n'.encode())
            s.recv(1024)
        s.sendall(f"{cmd}\r\n".encode())
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
            if b"\r\n" in resp:
                break
        return resp.decode(errors="replace")
    except (socket.error, socket.timeout, OSError) as e:
        logger.error("Tor control error: %s", e)
        return ""
    finally:
        s.close()


def _get_tor_bootstrap() -> int:
    resp = _tor_command("GETINFO status/bootstrap-phase")
    for line in resp.splitlines():
        if "PROGRESS=" in line:
            match = re.search(r"PROGRESS=(\d+)", line)
            if match:
                return int(match.group(1))
    return 0


def _get_tor_circuits() -> list:
    resp = _tor_command("GETINFO circuit-status")
    circuits = []
    for line in resp.splitlines():
        line = line.strip()
        if not line or line.startswith("250") or line.startswith("5"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        circuit_id = parts[0]
        status_str = parts[1]
        path_str = parts[2] if len(parts) > 2 else ""
        relays = []
        for hop in path_str.split(","):
            hop = hop.strip()
            if not hop:
                continue
            hop_parts = hop.split("~")
            fingerprint = hop_parts[0] if hop_parts else hop
            nickname = hop_parts[1] if len(hop_parts) > 1 else ""
            relays.append({
                "fingerprint": fingerprint,
                "nickname": nickname,
                "ip": "",
                "country_code": "",
                "type": "relay",
            })
        circuits.append({
            "id": circuit_id,
            "status": status_str,
            "path": relays,
        })
    return circuits


def _count_bridges() -> int:
    if not BRIDGES_CONF_FILE.exists():
        return 0
    count = 0
    with open(BRIDGES_CONF_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Bridge obfs4") and "REMPLACER" not in line:
                count += 1
    return count


def _is_wireguard_active() -> bool:
    result = subprocess.run(
        ["ip", "addr", "show", "wg0"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and "inet " in result.stdout


def _is_wifi_connected() -> bool:
    result = subprocess.run(
        ["ip", "addr", "show", "wlan0"],
        capture_output=True, text=True,
    )
    return "inet " in result.stdout


def _get_wifi_ssid() -> str:
    result = subprocess.run(
        ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi", "list", "--rescan", "no"],
        capture_output=True, text=True,
    )
    for line in result.stdout.strip().splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    if WIFI_CONF_FILE.exists():
        try:
            with open(WIFI_CONF_FILE, "r") as f:
                for line in f:
                    if line.strip().startswith('ssid='):
                        return line.strip().split("=", 1)[1].strip('"')
        except Exception:
            pass
    return ""


def _get_tier() -> int:
    if WG_CONF_FILE.exists():
        return 2
    return 1


def _get_led_color() -> str:
    if not SETUP_DONE_FILE.exists():
        return "blue_blink"
    bootstrap = _get_tor_bootstrap()
    if bootstrap < 100:
        return "red_blink"
    if _get_tier() == 2 and not _is_wireguard_active():
        return "orange"
    if bootstrap >= 100:
        return "green"
    return "orange"


def _get_wg_expiry() -> str:
    if not WG_EXPIRY_FILE.exists():
        return ""
    try:
        return WG_EXPIRY_FILE.read_text().strip()
    except Exception:
        return ""


def _is_noise_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "gygeslink-noise"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _is_jitter_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "gygeslink-jitter"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _get_uptime() -> int:
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0


def _get_recent_logs(lines: int = 50) -> list:
    result = subprocess.run(
        ["journalctl", "-n", str(lines), "--no-pager", "-o", "cat"],
        capture_output=True, text=True,
    )
    return result.stdout.strip().splitlines()


# ─────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "tor_bootstrap": _get_tor_bootstrap(),
        "tier": _get_tier(),
        "wireguard_active": _is_wireguard_active(),
        "wifi_connected": _is_wifi_connected(),
        "wifi_ssid": _get_wifi_ssid(),
        "bridges_count": _count_bridges(),
        "noise_active": _is_noise_active(),
        "jitter_active": _is_jitter_active(),
        "led_color": _get_led_color(),
        "wg_expiry": _get_wg_expiry(),
        "uptime_seconds": _get_uptime(),
        "setup_done": SETUP_DONE_FILE.exists(),
    })


@app.route("/api/config", methods=["GET"])
def api_config():
    tier = _get_tier()
    config = {
        "ssid": _get_wifi_ssid(),
        "tier": tier,
        "bridges_count": _count_bridges(),
        "wireguard": _is_wireguard_active(),
        "wg_expiry": _get_wg_expiry() if tier == 2 else "",
    }
    return jsonify(config)


@app.route("/api/wifi", methods=["POST"])
@limiter.limit("10 per minute")
def api_wifi():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")

    if not ssid:
        return _json_error("invalid_ssid", "Le SSID est obligatoire.")
    if len(ssid) > 32:
        return _json_error("invalid_ssid", "SSID trop long (32 caractères max).")
    if len(password) < 8:
        return _json_error("invalid_password", "Mot de passe trop court (8 caractères minimum).")
    if len(password) > 63:
        return _json_error("invalid_password", "Mot de passe trop long (63 caractères max).")
    if re.search(r'[\r\n\x00]', ssid) or re.search(r'[\r\n\x00]', password):
        return _json_error("invalid_chars", "Caractères invalides.")

    ssid_escaped = ssid.replace('"', '\\"')
    password_escaped = password.replace('"', '\\"')

    wifi_config = f'ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev\nupdate_config=1\n\nnetwork={{\n    ssid="{ssid_escaped}"\n    psk="{password_escaped}"\n    key_mgmt=WPA-PSK\n}}\n'

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WIFI_CONF_FILE.write_text(wifi_config)
    WIFI_CONF_FILE.chmod(0o600)

    nm_file = Path("/etc/NetworkManager/system-connections/GygesLink-WiFi.nmconnection")
    nm_file.parent.mkdir(parents=True, exist_ok=True)
    nm_file.write_text(
        f"[connection]\nid=GygesLink-WiFi\ntype=wifi\ninterface-name=wlan0\nautoconnect=true\n\n"
        f"[wifi]\nssid={ssid}\nmode=infrastructure\n\n"
        f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={password}\n\n"
        f"[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=disabled\n"
    )
    nm_file.chmod(0o600)

    subprocess.run(["nmcli", "connection", "reload"], capture_output=True)
    subprocess.run(["nmcli", "connection", "up", "GygesLink-WiFi"], capture_output=True)

    logger.info("WiFi configuré via API : SSID=%r", ssid)
    return _json_ok(message="WiFi configuré.")


@app.route("/api/bridges", methods=["POST"])
@limiter.limit("10 per minute")
def api_bridges():
    data = request.get_json(silent=True) or {}

    if data.get("skip"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BRIDGES_CONF_FILE.touch()
        BRIDGES_CONF_FILE.chmod(0o600)
        logger.info("Bridges ignoré via API.")
        return _json_ok(message="Bridges ignoré.")

    raw = data.get("bridges", "").strip()
    if not raw:
        return _json_error("empty_bridges", "Collez au moins une ligne de bridge obfs4.")

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    valid = []
    for line in lines:
        if line.lower().startswith("bridge "):
            line = line[7:]
        if not re.match(r'^obfs4\s+\d{1,3}(\.\d{1,3}){3}:\d{1,5}\s+\S+', line):
            continue
        if re.search(r'[\r\n\x00]', line):
            continue
        valid.append(f"Bridge {line}")

    if not valid:
        return _json_error("invalid_bridges", "Aucune bridge valide détectée. Format : obfs4 IP:PORT FINGERPRINT cert=...")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = "# GygesLink — Bridges obfs4 (API)\n" + "\n".join(valid) + "\n"
    BRIDGES_CONF_FILE.write_text(content)
    BRIDGES_CONF_FILE.chmod(0o600)

    logger.info("Bridges configurées via API : %d bridge(s).", len(valid))
    return _json_ok(message=f"{len(valid)} bridge(s) configurée(s).")


@app.route("/api/setup", methods=["POST"])
@limiter.limit("5 per minute")
def api_setup():
    data = request.get_json(silent=True) or {}
    tier = str(data.get("tier", "1"))
    account = data.get("account", "").strip()

    if tier == "1":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETUP_DONE_FILE.touch()
        logger.info("Setup Classic (Tier 1) via API.")
        subprocess.Popen(["bash", "-c", "sleep 3 && systemctl reboot"])
        return _json_ok(message="Mode Classic configuré. Redémarrage en cours…")

    if tier == "2":
        if not re.match(r"^\d{16}$", account):
            return _json_error("invalid_account", "Le numéro de compte Mullvad doit contenir exactement 16 chiffres.")

        try:
            private_key, public_key = _generate_wg_keys()
            import requests as req
            api_data = _register_wg_key(req, account, public_key)
            wg_config, expiry = _build_wg_config(private_key, api_data)

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            WG_CONF_FILE.write_text(wg_config)
            WG_CONF_FILE.chmod(0o600)
            WG_EXPIRY_FILE.write_text(expiry.isoformat())

            SETUP_DONE_FILE.touch()
            logger.info("Setup Advanced (Tier 2) via API. Expiry: %s", expiry.isoformat())
            subprocess.Popen(["bash", "-c", "sleep 3 && systemctl reboot"])
            return _json_ok(message="Mode Advanced configuré.", wg_expiry=expiry.isoformat())

        except ValueError as e:
            return _json_error("setup_error", str(e))
        except Exception as e:
            return _json_error("setup_error", f"Erreur lors de la configuration : {e}")

    return _json_error("invalid_tier", "Tier invalide (1 ou 2).")


def _generate_wg_keys() -> tuple:
    priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True)
    private_key = priv.stdout.strip()
    pub = subprocess.run(["wg", "pubkey"], input=private_key, capture_output=True, text=True, check=True)
    return private_key, pub.stdout.strip()


def _register_wg_key(req_lib, account: str, public_key: str) -> dict:
    url = "https://api.mullvad.net/wg/"
    resp = req_lib.post(url, data={"account": account, "pubkey": public_key}, timeout=30, verify=True)
    if resp.status_code == 400:
        raise ValueError("Compte Mullvad invalide ou expiré.")
    if resp.status_code == 401:
        raise ValueError("Authentification API Mullvad refusée.")
    if resp.status_code == 429:
        raise ValueError("Trop de clés enregistrées.")
    if resp.status_code != 200:
        raise RuntimeError(f"Erreur API Mullvad (HTTP {resp.status_code})")
    return resp.json()


def _build_wg_config(private_key: str, api_data: dict) -> tuple:
    try:
        server_pubkey = api_data["peers"][0]["public_key"]
    except (KeyError, IndexError, TypeError):
        server_pubkey = api_data.get("server", {}).get("public_key", "")
    if not server_pubkey:
        raise ValueError("Clé publique serveur Mullvad absente.")
    server_ip = api_data.get("server", {}).get("ipv4_addr_in", "")
    assigned_ip = api_data.get("ip", "")
    expiry_str = api_data.get("expiry", "")

    config = f"[Interface]\nPrivateKey = {private_key}\nAddress = {assigned_ip}/32\nDNS = 193.138.218.74\n\n[Peer]\nPublicKey = {server_pubkey}\nAllowedIPs = 0.0.0.0/0\nEndpoint = {server_ip}:51820\nPersistentKeepalive = 25\n"

    from datetime import timedelta
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
        except ValueError:
            expiry = datetime.now(timezone.utc) + timedelta(days=30)
    else:
        expiry = datetime.now(timezone.utc) + timedelta(days=30)

    return config, expiry


@app.route("/api/factory-reset", methods=["POST"])
@limiter.limit("2 per minute")
def api_factory_reset():
    files_to_delete = [
        SETUP_DONE_FILE,
        WIFI_CONF_FILE,
        WG_CONF_FILE,
        WG_EXPIRY_FILE,
        BRIDGES_CONF_FILE,
    ]
    for f in files_to_delete:
        if f.exists():
            f.unlink()

    nm_result = subprocess.run(
        ["nmcli", "-t", "-f", "TYPE,UUID", "connection", "show"],
        capture_output=True, text=True,
    )
    for line in nm_result.stdout.strip().splitlines():
        if "802-11-wireless" in line:
            uuid = line.split(":", 2)[-1] if ":" in line else ""
            if uuid:
                subprocess.run(["nmcli", "connection", "delete", uuid], capture_output=True)

    subprocess.Popen(["bash", "-c", "sleep 3 && systemctl reboot"])
    logger.info("Factory reset via API.")
    return _json_ok(message="Réinitialisation effectuée. Redémarrage en cours…")


@app.route("/api/logs", methods=["GET"])
def api_logs():
    try:
        n = int(request.args.get("n", "50"))
        n = max(1, min(n, 500))
    except ValueError:
        n = 50
    return jsonify(_get_recent_logs(n))


@app.route("/api/tor/circuit", methods=["GET"])
def api_tor_circuit():
    circuits = _get_tor_circuits()
    built = [c for c in circuits if c.get("status") == "BUILT"]
    return jsonify({
        "status": "ok" if built else "no_active_circuit",
        "circuit": built[0] if built else (circuits[0] if circuits else None),
    })


@app.route("/api/tor/new-identity", methods=["POST"])
@limiter.limit("6 per minute")
def api_tor_new_identity():
    resp = _tor_command("SIGNAL NEWNYM")
    if "250" in resp:
        return _json_ok(message="Nouveau circuit Tor demandé.")
    return _json_error("tor_error", "Impossible de demander un nouveau circuit.", 503)


@app.route("/api/reboot", methods=["POST"])
@limiter.limit("2 per minute")
def api_reboot():
    subprocess.Popen(["bash", "-c", "sleep 2 && systemctl reboot"])
    return _json_ok(message="Redémarrage en cours…")


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    logger.info("API GygesLink démarrée sur http://%s:%d", API_HOST, API_PORT)
    app.run(host=API_HOST, port=API_PORT, debug=False, threaded=True)