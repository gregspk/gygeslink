#!/usr/bin/env python3
"""
GygesLink — API REST pour l'app desktop

Sert les endpoints /api/* pour la gestion à distance du boîtier.
Écoute sur 192.168.100.1:4430 (HTTP, lien USB uniquement).
Le portail setup reste sur :443 (HTTPS).

Fonctionne EN PARALLÈLE du portail setup :
  - Si setup-done absent : portail ET api actifs
  - Si setup-done présent : seul l'api est actif (tor+iptables opérationnels)

Architecture SSE :
  - Thread daemon collecte le statut toutes les 5s dans un cache partagé
  - GET /api/status/stream : SSE, pousse un event quand le statut change
  - GET /api/status : lit le cache (instantané, plus de subprocesses)
  - Heartbeat ping toutes les 30s sur le stream
"""

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

NETPLAN_WIFI_FILE = Path("/etc/netplan/30-wifis-dhcp.yaml")

DATA_DIR         = Path("/data/gygeslink")
SETUP_DONE_FILE  = DATA_DIR / "setup-done"
WIFI_CONF_FILE   = DATA_DIR / "wifi.conf"
BRIDGES_CONF_FILE = DATA_DIR / "bridges.conf"
BRIDGES_CONF_PERM = 0o644
PAUSED_FILE       = DATA_DIR / "paused"

VERSION_FILE      = DATA_DIR / "version.txt"
DOWNLOAD_DIR      = DATA_DIR / "updates"
STATUS_FILE       = DATA_DIR / "update-status.json"
UPDATE_PUBKEY     = Path("/etc/gygeslink/update-pubkey.gpg")
GITHUB_REPO       = "gregspk/gygeslink"

API_HOST = "192.168.100.1"
API_PORT = 4430

TOR_CONTROL_HOST = "127.0.0.1"
TOR_CONTROL_PORT = 9051
TOR_COOKIE_PATHS = [
    "/var/lib/tor/control_auth_cookie",
    "/var/run/tor/control.authcookie",
    "/run/tor/control.authcookie",
]

STATUS_CACHE_INTERVAL = 5
SSE_PING_INTERVAL = 30

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
    default_limits=["10000 per day"],
    storage_uri="memory://",
)

# ─────────────────────────────────────────────────────────────────────
# Status cache — thread-safe, mis à jour en arrière-plan
# ─────────────────────────────────────────────────────────────────────

_status_cache = {}
_status_lock = threading.Lock()


_update_available = False
_update_version = ""


def _collect_status() -> dict:
    return {
        "tor_bootstrap": _get_tor_bootstrap(),
        "wifi_connected": _is_wifi_connected(),
        "wifi_ssid": _get_wifi_ssid(),
        "bridges_count": _count_bridges(),
        "noise_active": _is_noise_active(),
        "jitter_active": _is_jitter_active(),
        "led_color": _get_led_color(),
        "uptime_seconds": _get_uptime(),
        "setup_done": SETUP_DONE_FILE.exists(),
        "paused": PAUSED_FILE.exists(),
        "pi_update_available": _update_available,
        "pi_update_version": _update_version,
    }


def _status_collector():
    global _status_cache
    while True:
        try:
            new_status = _collect_status()
            with _status_lock:
                _status_cache = new_status
        except Exception as e:
            logger.error("Status collector error: %s", e)
        time.sleep(STATUS_CACHE_INTERVAL)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _json_ok(**kwargs) -> tuple:
    return jsonify({"success": True, **kwargs}), 200


def _json_error(error: str, message: str, status: int = 400) -> tuple:
    return jsonify({"success": False, "error": error, "message": message}), status


def _tor_cookie_auth() -> str:
    for path in TOR_COOKIE_PATHS:
        try:
            with open(path, "rb") as f:
                cookie = f.read()
            if cookie:
                logger.info("Tor cookie found at %s", path)
                return cookie.hex()
        except (OSError, IOError):
            continue
    logger.warning("Tor cookie not found in any path")
    return ""


def _tor_command(cmd: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((TOR_CONTROL_HOST, TOR_CONTROL_PORT))
        cookie_hex = _tor_cookie_auth()
        if cookie_hex:
            auth_cmd = f'AUTHENTICATE "{cookie_hex}"\r\n'
        else:
            auth_cmd = 'AUTHENTICATE ""\r\n'
        s.sendall(auth_cmd.encode())
        auth_resp = _tor_read_line(s)
        if "250" not in auth_resp:
            logger.error("Tor AUTHENTICATE failed: %s", auth_resp.strip())
            return ""
        s.sendall(f"{cmd}\r\n".encode())
        resp = _tor_read_response(s)
        return resp
    except (socket.error, socket.timeout, OSError) as e:
        logger.error("Tor control error: %s", e)
        return ""
    finally:
        s.close()


def _tor_read_line(s: socket.socket) -> str:
    data = b""
    while b"\r\n" not in data:
        chunk = s.recv(1024)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace")


def _tor_read_response(s: socket.socket) -> str:
    resp = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        resp += chunk
        text = resp.decode(errors="replace")
        if text.endswith("250 OK\r\n"):
            break
        if re.search(r"^5\d{2} ", text, re.MULTILINE):
            break
    return resp.decode(errors="replace")


def _get_tor_bootstrap() -> int:
    result = subprocess.run(
        ["ss", "-tlnp"],
        capture_output=True, text=True,
    )
    if ":9040 " in result.stdout:
        return 100
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


def _is_wifi_connected() -> bool:
    result = subprocess.run(
        ["iw", "dev", "wlan0", "link"],
        capture_output=True, text=True, timeout=5,
    )
    return "Connected to" in result.stdout


def _get_wifi_ssid() -> str:
    result = subprocess.run(
        ["iw", "dev", "wlan0", "link"],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            return line.split("SSID:", 1)[1].strip()
    if WIFI_CONF_FILE.exists():
        try:
            with open(WIFI_CONF_FILE, "r") as f:
                for line in f:
                    if line.strip().startswith('ssid='):
                        return line.strip().split("=", 1)[1].strip('"')
        except Exception:
            pass
    return ""


def _get_led_color() -> str:
    if PAUSED_FILE.exists():
        return "pause"
    if not SETUP_DONE_FILE.exists():
        return "blue_blink"
    with _status_lock:
        bootstrap = _status_cache.get("tor_bootstrap", 0)
    if bootstrap < 100:
        return "red_blink"
    if bootstrap >= 100:
        return "blue"
    return "orange"


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
@limiter.exempt
def api_status():
    with _status_lock:
        return jsonify(dict(_status_cache))


@app.route("/api/status/stream", methods=["GET"])
@limiter.exempt
def api_status_stream():
    def generate():
        with _status_lock:
            initial = dict(_status_cache)
            last_json = json.dumps(initial, sort_keys=True)
        yield f"event: status\ndata: {json.dumps(initial)}\n\n"

        last_hash = hash(last_json)
        last_ping = time.time()

        while True:
            time.sleep(1)
            now = time.time()

            if now - last_ping >= SSE_PING_INTERVAL:
                yield "event: ping\ndata: \n\n"
                last_ping = now

            with _status_lock:
                current = dict(_status_cache)
            current_json = json.dumps(current, sort_keys=True)
            current_hash = hash(current_json)

            if current_hash != last_hash:
                yield f"event: status\ndata: {json.dumps(current)}\n\n"
                last_hash = current_hash
                last_ping = now

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/config", methods=["GET"])
def api_config():
    config = {
        "ssid": _get_wifi_ssid(),
        "bridges_count": _count_bridges(),
    }
    return jsonify(config)


@app.route("/api/wifi/scan", methods=["GET"])
@limiter.limit("2 per minute")
def api_wifi_scan():
    networks = _wifi_scan_iw()
    return jsonify({"networks": networks})


def _wifi_scan_iw() -> list:
    subprocess.run(["ip", "link", "set", "wlan0", "up"], capture_output=True, timeout=5)
    result = subprocess.run(
        ["iw", "dev", "wlan0", "scan"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        logger.warning("iw scan failed: %s", result.stderr.strip())
        return []
    networks = []
    current = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("BSS "):
            if current.get("ssid"):
                networks.append(current)
            current = {}
        elif stripped.startswith("SSID:"):
            current["ssid"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("signal:"):
            try:
                dbm = float(stripped.split(":")[1].strip().split()[0])
                quality = min(100, max(0, int((dbm + 100) * 2)))
            except (ValueError, IndexError):
                quality = 0
            current["signal"] = quality
        elif stripped.startswith("RSN:"):
            current["security"] = "WPA2"
        elif stripped.startswith("WPA:"):
            if current.get("security") != "WPA2":
                current["security"] = "WPA1"
        elif stripped.startswith("capability:") and "Privacy" in stripped and "security" not in current:
            current["security"] = "WEP"
    if current.get("ssid"):
        networks.append(current)
    for n in networks:
        if "security" not in n:
            n["security"] = "Open"
    seen = {}
    for n in networks:
        ssid = n["ssid"]
        if ssid not in seen or n["signal"] > seen[ssid]["signal"]:
            seen[ssid] = n
    return sorted(seen.values(), key=lambda x: x["signal"], reverse=True)


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

    netplan_yaml = (
        "network:\n"
        "  version: 2\n"
        "  renderer: networkd\n"
        "  wifis:\n"
        "    wlan0:\n"
        "      dhcp4: true\n"
        "      macaddress: shuffle\n"
        f'      access-points:\n'
        f'        "{ssid_escaped}":\n'
        f'          password: "{password_escaped}"\n'
    )
    NETPLAN_WIFI_FILE.write_text(netplan_yaml)
    NETPLAN_WIFI_FILE.chmod(0o600)

    apply_result = subprocess.run(
        ["netplan", "apply"], capture_output=True, text=True, timeout=30,
    )
    if apply_result.returncode != 0:
        logger.error("netplan apply failed: %s", apply_result.stderr.strip())
        return _json_error("netplan_error", f"netplan apply failed: {apply_result.stderr.strip()[:200]}", 500)

    connected = False
    for _ in range(15):
        time.sleep(1)
        r = subprocess.run(["ip", "addr", "show", "wlan0"], capture_output=True, text=True)
        if "inet " in r.stdout:
            connected = True
            break

    nm_file = Path("/etc/NetworkManager/system-connections/GygesLink-WiFi.nmconnection")
    if nm_file.exists():
        nm_file.unlink()

    logger.info("WiFi configuré via API (netplan) : SSID=%r connected=%s", ssid, connected)
    return _json_ok(message="WiFi configuré.")


@app.route("/api/bridges", methods=["POST"])
@limiter.limit("10 per minute")
def api_bridges():
    data = request.get_json(silent=True) or {}

    if data.get("skip"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BRIDGES_CONF_FILE.write_text("# GygesLink — Bridges obfs4\n")
        BRIDGES_CONF_FILE.chmod(BRIDGES_CONF_PERM)
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
    BRIDGES_CONF_FILE.chmod(BRIDGES_CONF_PERM)

    logger.info("Bridges configurées via API : %d bridge(s).", len(valid))
    return _json_ok(message=f"{len(valid)} bridge(s) configurée(s).")


@app.route("/api/setup", methods=["POST"])
@limiter.limit("5 per minute")
def api_setup():
    data = request.get_json(silent=True) or {}
    tier = str(data.get("tier", "1"))

    if tier != "1":
        return _json_error("invalid_tier", "Tier 1 uniquement.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_DONE_FILE.touch()
    logger.info("Setup Classic (Tier 1) via API.")
    subprocess.Popen(["bash", "-c", "sleep 3 && systemctl reboot"])
    return _json_ok(message="Mode Classic configuré. Redémarrage en cours…")


@app.route("/api/pause", methods=["POST"])
@limiter.limit("6 per minute")
def api_pause():
    if PAUSED_FILE.exists():
        return _json_error("already_paused", "Le mode pause est déjà actif.")

    if not _is_wifi_connected():
        return _json_error("no_wifi", "Aucune connexion WiFi active. Le mode pause nécessite un accès Internet via le routeur.")

    try:
        subprocess.run(
            ["/usr/local/bin/gygeslink-iptables.sh", "bypass"],
            capture_output=True, text=True, timeout=15,
        )
        subprocess.run(
            ["systemctl", "stop", "gygeslink-noise"],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["systemctl", "stop", "gygeslink-jitter"],
            capture_output=True, text=True, timeout=10,
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PAUSED_FILE.touch()
        logger.info("Mode pause activé via API.")
        return _json_ok(message="Mode pause activé. Le trafic n'est plus anonymisé.")
    except Exception as e:
        logger.error("Erreur pause: %s", e)
        return _json_error("pause_error", f"Erreur lors de l'activation du mode pause : {e}", 500)


@app.route("/api/resume", methods=["POST"])
@limiter.limit("6 per minute")
def api_resume():
    if not PAUSED_FILE.exists():
        return _json_error("not_paused", "Le mode pause n'est pas actif.")

    try:
        subprocess.run(
            ["/usr/local/bin/gygeslink-iptables.sh", "open"],
            capture_output=True, text=True, timeout=15,
        )
        subprocess.run(
            ["systemctl", "start", "gygeslink-noise"],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["systemctl", "start", "gygeslink-jitter"],
            capture_output=True, text=True, timeout=10,
        )
        PAUSED_FILE.unlink(missing_ok=True)
        logger.info("Mode Tor rétabli via API.")
        return _json_ok(message="Mode Tor rétabli. Le trafic est de nouveau anonymisé.")
    except Exception as e:
        logger.error("Erreur resume: %s", e)
        return _json_error("resume_error", f"Erreur lors de la reprise du mode Tor : {e}", 500)


@app.route("/api/factory-reset", methods=["POST"])
@limiter.limit("2 per minute")
def api_factory_reset():
    files_to_delete = [
        SETUP_DONE_FILE,
        WIFI_CONF_FILE,
        PAUSED_FILE,
        NETPLAN_WIFI_FILE,
    ]
    for f in files_to_delete:
        if f.exists():
            f.unlink()

    BRIDGES_CONF_FILE.write_text("# GygesLink — Bridges obfs4\n")
    BRIDGES_CONF_FILE.chmod(BRIDGES_CONF_PERM)

    subprocess.run(["netplan", "apply"], capture_output=True, timeout=30)

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
    return jsonify({"status": "ok", "circuit": None})


@app.route("/api/tor/new-identity", methods=["POST"])
@limiter.limit("6 per minute")
def api_tor_new_identity():
    resp = _tor_command("SIGNAL NEWNYM")
    if "250" in resp:
        return _json_ok(message="Nouveau circuit Tor demandé.")
    logger.warning("NEWNYM via ControlPort failed, falling back to Tor restart.")
    try:
        subprocess.run(["systemctl", "restart", "gygeslink-tor"], capture_output=True, timeout=10)
        return _json_ok(message="Nouveau circuit Tor en cours de construction (restart).")
    except Exception as e:
        logger.error("Tor restart failed: %s", e)
        return _json_error("tor_error", "Impossible de demander un nouveau circuit.", 503)


@app.route("/api/reboot", methods=["POST"])
@limiter.limit("2 per minute")
def api_reboot():
    subprocess.Popen(["bash", "-c", "sleep 2 && systemctl reboot"])
    return _json_ok(message="Redémarrage en cours…")


@app.route("/api/health", methods=["GET"])
@limiter.exempt
def api_health():
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────────────
# Mise à jour OTA
# ─────────────────────────────────────────────────────────────────────

def _get_current_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "0.0.0"


def _check_github_release() -> dict:
    import requests as req
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        resp = req.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        latest = data.get("tag_name", "").lstrip("v")
        changelog = data.get("body", "")
        assets = data.get("assets", [])
        download_url = ""
        checksums_url = ""
        checksums_sig_url = ""
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(".tar.gz"):
                download_url = asset.get("browser_download_url", "")
            elif name == "SHA256SUMS":
                checksums_url = asset.get("browser_download_url", "")
            elif name == "SHA256SUMS.sig":
                checksums_sig_url = asset.get("browser_download_url", "")
        current = _get_current_version()
        available = latest > current
        return {
            "available": available,
            "current": current,
            "version": latest,
            "changelog": changelog,
            "download_url": download_url,
            "checksums_url": checksums_url,
            "checksums_sig_url": checksums_sig_url,
        }
    except Exception as e:
        logger.error("Update check failed: %s", e)
        return {"available": False, "error": str(e)}


@app.route("/api/update/check", methods=["GET"])
@limiter.limit("2 per minute")
def api_update_check():
    global _update_available, _update_version
    import requests as req
    info = _check_github_release()
    if "error" in info:
        return _json_error("check_error", info["error"], 500)
    _update_available = info["available"]
    _update_version = info.get("version", "")
    return jsonify({
        "available": info["available"],
        "current": info.get("current", _get_current_version()),
        "version": _update_version,
        "changelog": info.get("changelog", ""),
    })


@app.route("/api/update/apply", methods=["POST"])
@limiter.limit("1 per hour")
def api_update_apply():
    info = _check_github_release()
    if not info.get("available"):
        return _json_error("no_update", "Aucune mise à jour disponible.")

    download_url = info.get("download_url", "")
    checksums_url = info.get("checksums_url", "")
    checksums_sig_url = info.get("checksums_sig_url", "")
    version = info.get("version", "unknown")

    if not download_url:
        return _json_error("no_asset", "Archive introuvable sur GitHub.")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    _write_update_status("downloading", 10, version, "Téléchargement en cours...")

    try:
        archive_name = download_url.split("/")[-1]
        archive_path = DOWNLOAD_DIR / archive_name

        logger.info("Téléchargement de %s via Tor...", archive_name)
        result = subprocess.run(
            [
                "curl", "--socks5-hostname", "127.0.0.1:9050",
                "-L", "-o", str(archive_path),
                "-s", "--show-error",
                download_url,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            _write_update_status("error", 0, "", f"curl failed: {result.stderr[:200]}")
            return _json_error("download_error", f"Téléchargement échoué : {result.stderr[:200]}", 500)

        if checksums_url:
            checksums_path = DOWNLOAD_DIR / "SHA256SUMS"
            r_cs = subprocess.run(
                [
                    "curl", "--socks5-hostname", "127.0.0.1:9050",
                    "-L", "-s", "--show-error",
                    "-o", str(checksums_path),
                    checksums_url,
                ],
                capture_output=True, text=True, timeout=60,
            )
            if r_cs.returncode != 0:
                logger.error("Failed to download SHA256SUMS via Tor: %s", r_cs.stderr[:200])

        if checksums_sig_url:
            sig_path = DOWNLOAD_DIR / "SHA256SUMS.sig"
            r_sig = subprocess.run(
                [
                    "curl", "--socks5-hostname", "127.0.0.1:9050",
                    "-L", "-s", "--show-error",
                    "-o", str(sig_path),
                    checksums_sig_url,
                ],
                capture_output=True, text=True, timeout=60,
            )
            if r_sig.returncode != 0:
                logger.error("Failed to download SHA256SUMS.sig via Tor: %s", r_sig.stderr[:200])

        _write_update_status("verifying", 40, version, "Vérification de la signature...")

        if not UPDATE_PUBKEY.exists():
            _write_update_status("error", 0, version, "Clé publique GPG introuvable")
            return _json_error("no_pubkey", "Clé publique GPG introuvable sur le boîtier.", 500)

        logger.info("Lancement de la mise à jour v%s...", version)
        _write_update_status("installing", 60, version, "Installation...")

        subprocess.Popen(
            [
                "/usr/local/bin/gygeslink-update.sh",
                str(archive_path),
                version,
            ],
            start_new_session=True,
        )

        return _json_ok(message=f"Mise à jour v{version} en cours. Le boîtier va redémarrer.")

    except Exception as e:
        logger.error("Update apply error: %s", e)
        _write_update_status("error", 0, "", str(e))
        return _json_error("update_error", f"Erreur lors de la mise à jour : {e}", 500)


@app.route("/api/update/status", methods=["GET"])
@limiter.exempt
def api_update_status():
    try:
        data = json.loads(STATUS_FILE.read_text())
        return jsonify(data)
    except Exception:
        return jsonify({"status": "idle", "progress": 0, "version": "", "message": ""})


def _write_update_status(status: str, progress: int, version: str = "", message: str = ""):
    STATUS_FILE.write_text(json.dumps({
        "status": status,
        "progress": progress,
        "version": version,
        "message": message,
    }))


if __name__ == "__main__":
    collector = threading.Thread(target=_status_collector, daemon=True)
    collector.start()
    logger.info("API GygesLink démarrée sur http://%s:%d", API_HOST, API_PORT)
    app.run(host=API_HOST, port=API_PORT, debug=False, threaded=True)