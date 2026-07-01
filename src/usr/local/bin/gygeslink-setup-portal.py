#!/usr/bin/env python3
"""
GygesLink - Captive portal de setup

Serveur HTTPS Flask pour la configuration initiale du boîtier.
Actif uniquement au premier boot (absence de /data/gygeslink/setup-done).

Flux :
  1. L'user branche le boîtier en USB-C
  2. Le navigateur détecte le portail (réponses aux sondes captive portal)
  3. L'user configure le WiFi, les bridges obfs4, puis valide
  4. Le portail écrit la configuration et reboot

Sécurité :
  - HTTPS avec certificat auto-signé (généré au premier lancement)
  - Validation stricte des entrées
  - Rate limiting : 5 tentatives POST par minute par IP
  - Le port 80 est redirigé vers 443 par iptables (voir gygeslink-setup.service)
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from flask import Flask, request, redirect, render_template_string
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

PORTAL_HOST      = "192.168.100.1"
PORTAL_PORT      = 443
CERT_FILE        = "/etc/gygeslink/setup-cert.pem"
KEY_FILE         = "/etc/gygeslink/setup-key.pem"

DATA_DIR         = Path("/data/gygeslink")
SETUP_DONE_FILE  = DATA_DIR / "setup-done"
WIFI_CONF_FILE   = DATA_DIR / "wifi.conf"
BRIDGES_CONF_FILE = DATA_DIR / "bridges.conf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [setup-portal] %(levelname)s %(message)s",
)
logger = logging.getLogger("setup-portal")


# ─────────────────────────────────────────────────────────────────────
# Flask app + rate limiter
# ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(32)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day"],
    storage_uri="memory://",
)


# ─────────────────────────────────────────────────────────────────────
# Templates HTML
# ─────────────────────────────────────────────────────────────────────

HTML_BASE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GygesLink — Setup</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f0f;
      color: #e0e0e0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: #1a1a1a;
      border: 1px solid #333;
      border-radius: 12px;
      padding: 32px;
      max-width: 480px;
      width: 100%;
    }
    .logo {
      font-size: 28px;
      font-weight: 700;
      color: #00ff88;
      margin-bottom: 8px;
    }
    .subtitle {
      color: #888;
      font-size: 14px;
      margin-bottom: 28px;
    }
    h2 { font-size: 18px; margin-bottom: 16px; color: #ccc; }
    .form-group { margin-bottom: 20px; }
    label { display: block; margin-bottom: 6px; font-size: 14px; color: #aaa; }
    input[type="text"], input[type="password"] {
      width: 100%;
      padding: 12px;
      background: #111;
      border: 1px solid #444;
      border-radius: 6px;
      color: #e0e0e0;
      font-size: 16px;
      letter-spacing: 2px;
    }
    input:focus { outline: none; border-color: #00ff88; }
    .btn-primary {
      display: block;
      width: 100%;
      padding: 14px;
      background: #00ff88;
      color: #000;
      border: none;
      border-radius: 8px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
    }
    .btn-primary:hover { background: #00cc6a; }
    .btn-secondary {
      display: block;
      width: 100%;
      padding: 12px;
      background: transparent;
      color: #888;
      border: 1px solid #444;
      border-radius: 8px;
      font-size: 14px;
      cursor: pointer;
      margin-top: 10px;
    }
    .alert {
      padding: 12px 16px;
      border-radius: 6px;
      margin-bottom: 20px;
      font-size: 14px;
    }
    .alert-error   { background: #2d1b1b; border: 1px solid #7f2020; color: #ff8888; }
    .alert-success { background: #1b2d1b; border: 1px solid #207f20; color: #88ff88; }
    .alert-info    { background: #1b1b2d; border: 1px solid #20207f; color: #8888ff; }
    .hint { font-size: 12px; color: #666; margin-top: 6px; }
    .tier-btn {
      display: block;
      width: 100%;
      padding: 16px 20px;
      margin-bottom: 12px;
      border: 1px solid #444;
      border-radius: 8px;
      background: #222;
      color: #e0e0e0;
      cursor: pointer;
      text-align: left;
      font-size: 15px;
      transition: border-color 0.2s, background 0.2s;
    }
    .tier-btn:hover { border-color: #00ff88; background: #1f2f1f; }
    .tier-btn .tier-name { font-weight: 600; font-size: 16px; }
    .tier-btn .tier-desc { color: #888; font-size: 13px; margin-top: 4px; }
  </style>
</head>
<body>
<div class="card">
  <div class="logo">GygesLink</div>
  <div class="subtitle">Configuration initiale</div>
  {% block content %}{% endblock %}
</div>
</body>
</html>"""

HTML_HOME = HTML_BASE.replace(
    "{% block content %}{% endblock %}",
    """
  {% if error %}
  <div class="alert alert-error">{{ error }}</div>
  {% endif %}

  <h2>Activer la protection</h2>

  <form method="post" action="/setup">
    <input type="hidden" name="tier" value="1">
    <button type="submit" class="tier-btn">
      <div class="tier-name">Classic</div>
      <div class="tier-desc">Tor + obfs4 + jitter + bruit de fond</div>
    </button>
  </form>
"""
)

HTML_SUCCESS = HTML_BASE.replace(
    "{% block content %}{% endblock %}",
    """
  <div class="alert alert-success">{{ message }}</div>
  <p style="color:#888; font-size:14px; line-height:1.6;">
    Le boîtier va redémarrer dans quelques secondes.<br>
    La LED deviendra <strong style="color:#00ff88">bleue</strong> une fois la protection active.<br><br>
    Le câble USB-C peut rester branché — la protection est active dès le redémarrage.
  </p>
"""
)

HTML_WIFI = HTML_BASE.replace(
    "{% block content %}{% endblock %}",
    """
  {% if error %}
  <div class="alert alert-error">{{ error }}</div>
  {% endif %}

  <h2>Connexion WiFi</h2>
  <p style="color:#888; font-size:13px; margin-bottom:20px;">
    Le boîtier se connecte à votre routeur via WiFi.<br>
    Entrez les identifiants de votre réseau domestique.
  </p>

  <form method="post" action="/wifi">
    <div class="form-group">
      <label for="ssid">Nom du réseau (SSID)</label>
      <input type="text" id="ssid" name="ssid"
             placeholder="MonWiFi" maxlength="32"
             autocomplete="off" autocapitalize="none" required>
    </div>
    <div class="form-group">
      <label for="password">Mot de passe</label>
      <input type="password" id="password" name="password"
             placeholder="••••••••" maxlength="63"
             autocomplete="off" required>
      <div class="hint">WPA2 — minimum 8 caractères</div>
    </div>
    <button type="submit" class="btn-primary">Continuer →</button>
  </form>
"""
)

HTML_BRIDGES = HTML_BASE.replace(
    "{% block content %}{% endblock %}",
    """
  {% if error %}
  <div class="alert alert-error">{{ error }}</div>
  {% endif %}
  {% if info %}
  <div class="alert alert-info">{{ info }}</div>
  {% endif %}

  <h2>Bridges obfs4</h2>
  <p style="color:#888; font-size:13px; margin-bottom:20px;">
    Les bridges obfs4 masquent le trafic Tor pour échapper au DPI.<br>
    Obtenez-les sur <strong>bridges.torproject.org</strong> (3 recommandés).<br>
    Vous pouvez ignorer cette étape si vous n'êtes pas censuré.
  </p>

  <form method="post" action="/bridges">
    <div class="form-group">
      <label for="bridges">Lignes de bridge (une par ligne)</label>
      <textarea id="bridges" name="bridges"
                rows="6" style="width:100%;padding:12px;background:#111;border:1px solid #444;border-radius:6px;color:#e0e0e0;font-size:13px;font-family:monospace;resize:vertical;"
                placeholder="obfs4 1.2.3.4:443 ABCDEF cert=aBcDeFgH... iat-mode=0
obfs4 5.6.7.8:443 GHIJKL cert=xYzAbC... iat-mode=0">{{ existing_bridges }}</textarea>
      <div class="hint">Collez ici les lignes obtenues sur bridges.torproject.org</div>
    </div>
    <button type="submit" class="btn-primary">Enregistrer les bridges</button>
    <button type="submit" name="skip" value="1" class="btn-secondary">Passer cette étape →</button>
  </form>
"""
)


# ─────────────────────────────────────────────────────────────────────
# Génération du certificat SSL (si absent)
# ─────────────────────────────────────────────────────────────────────

def ensure_ssl_cert() -> None:
    """Génère un certificat auto-signé si absent."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return

    logger.info("Génération du certificat SSL auto-signé...")
    os.makedirs(os.path.dirname(CERT_FILE), exist_ok=True)

    result = subprocess.run(
        [
            "openssl", "req", "-x509",
            "-newkey", "rsa:2048",
            "-keyout", KEY_FILE,
            "-out", CERT_FILE,
            "-days", "365",
            "-nodes",
            "-subj", f"/CN=GygesLink-Setup/O=GygesLink/C=FR",
            "-addext", "subjectAltName=IP:192.168.100.1",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("Échec génération SSL : %s", result.stderr)
        sys.exit(1)

    logger.info("Certificat SSL généré : %s", CERT_FILE)


# ─────────────────────────────────────────────────────────────────────
# Routes Flask
# ─────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """Page d'accueil du portail.
    Flux : WiFi → Bridges obfs4 → Activation.
    """
    if not WIFI_CONF_FILE.exists():
        return render_template_string(HTML_WIFI, error=None)
    if not BRIDGES_CONF_FILE.exists():
        existing = ""
        return render_template_string(HTML_BRIDGES, error=None, info=None, existing_bridges=existing)
    return render_template_string(HTML_HOME, error=None)


@app.route("/wifi", methods=["POST"])
@limiter.limit("10 per minute")
def wifi():
    """Enregistre les credentials WiFi dans /data/gygeslink/wifi.conf et /etc/netplan/."""
    ssid     = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")

    if not ssid:
        return render_template_string(HTML_WIFI, error="Le nom du réseau (SSID) est obligatoire.")
    if len(ssid) > 32:
        return render_template_string(HTML_WIFI, error="SSID trop long (32 caractères max).")
    if len(password) < 8:
        return render_template_string(HTML_WIFI, error="Mot de passe trop court (8 caractères minimum pour WPA2).")
    if len(password) > 63:
        return render_template_string(HTML_WIFI, error="Mot de passe trop long (63 caractères max).")
    if re.search(r'[\r\n\x00]', ssid) or re.search(r'[\r\n\x00]', password):
        return render_template_string(HTML_WIFI, error="Caractères invalides dans le SSID ou le mot de passe.")

    ssid_escaped     = ssid.replace('"', '\\"')
    password_escaped = password.replace('"', '\\"')

    wifi_config = f"""ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1

network {{
    ssid="{ssid_escaped}"
    psk="{password_escaped}"
    key_mgmt=WPA-PSK
}}
"""

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
    netplan_file = Path("/etc/netplan/30-wifis-dhcp.yaml")
    netplan_file.write_text(netplan_yaml)
    netplan_file.chmod(0o600)

    apply_result = subprocess.run(
        ["netplan", "apply"], capture_output=True, text=True, timeout=30,
    )
    if apply_result.returncode != 0:
        logger.error("netplan apply failed: %s", apply_result.stderr.strip())

    import time as _time
    for _ in range(15):
        _time.sleep(1)
        r = subprocess.run(["ip", "addr", "show", "wlan0"], capture_output=True, text=True)
        if "inet " in r.stdout:
            logger.info("WiFi connecté via netplan : SSID=%r", ssid)
            break

    nm_file = Path("/etc/NetworkManager/system-connections/GygesLink-WiFi.nmconnection")
    if nm_file.exists():
        nm_file.unlink()

    logger.info("WiFi configuré : SSID=%r", ssid)

    existing = ""
    return render_template_string(HTML_BRIDGES, error=None, info=None, existing_bridges=existing)


@app.route("/bridges", methods=["POST"])
@limiter.limit("10 per minute")
def bridges():
    """Enregistre les bridges obfs4 dans /data/gygeslink/bridges.conf."""
    if request.form.get("skip"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BRIDGES_CONF_FILE.write_text("# GygesLink — Bridges obfs4\n")
        BRIDGES_CONF_FILE.chmod(0o644)
        logger.info("Bridges obfs4 : ignoré par l'utilisateur.")
        return render_template_string(HTML_HOME, error=None)

    raw = request.form.get("bridges", "").strip()

    if not raw:
        return render_template_string(HTML_BRIDGES, error="Collez au moins une ligne de bridge obfs4.", info=None, existing_bridges="")

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
        return render_template_string(
            HTML_BRIDGES,
            error="Aucune bridge valide détectée. Format attendu : obfs4 IP:PORT FINGERPRINT cert=...",
            info=None,
            existing_bridges=raw,
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = "# GygesLink — Bridges obfs4 (configurées via portail)\n" + "\n".join(valid) + "\n"
    BRIDGES_CONF_FILE.write_text(content)
    BRIDGES_CONF_FILE.chmod(0o644)

    logger.info("Bridges obfs4 configurées : %d bridge(s).", len(valid))

    return render_template_string(HTML_HOME, error=None)


# ── Sondes captive portal des OS mobiles ─────────────────────────────

@app.route("/generate_204")
def android_captive():
    """Android vérifie l'accès internet avec cette URL (attend HTTP 204)."""
    return redirect(f"https://{PORTAL_HOST}/", code=302)


@app.route("/hotspot-detect.html")
@app.route("/library/test/success.html")
def ios_captive():
    """iOS vérifie le portail avec ces URLs."""
    return redirect(f"https://{PORTAL_HOST}/", code=302)


@app.route("/connecttest.txt")
def windows_captive():
    """Windows vérifie avec cette URL."""
    return redirect(f"https://{PORTAL_HOST}/", code=302)


# ── Traitement du formulaire de setup ────────────────────────────────

@app.route("/setup", methods=["POST"])
@limiter.limit("5 per minute")
def setup():
    """
    Traite le formulaire de configuration.
    Écrit setup-done et reboot.
    """
    tier = request.form.get("tier", "1")

    if tier != "1":
        return render_template_string(HTML_HOME, error="Tier invalide.")

    logger.info("Setup Classic (Tier 1).")
    _finalize_setup()
    return render_template_string(
        HTML_SUCCESS,
        message="Mode Classic configuré. Redémarrage en cours…",
    )


def _finalize_setup() -> None:
    """
    Finalise le setup : écrit setup-done et déclenche le reboot.
    Le reboot est lancé en arrière-plan pour laisser le temps à Flask
    de renvoyer la réponse HTTP au navigateur.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_DONE_FILE.touch()
    logger.info("Fichier setup-done écrit : %s", SETUP_DONE_FILE)

    subprocess.Popen(["bash", "-c", "sleep 3 && systemctl reboot"])
    logger.info("Reboot programmé dans 3 secondes.")


# ─────────────────────────────────────────────────────────────────────
# Gestion des erreurs Flask
# ─────────────────────────────────────────────────────────────────────

@app.errorhandler(429)
def rate_limit_exceeded(e):
    return render_template_string(
        HTML_HOME,
        error="Trop de tentatives. Attendez 1 minute avant de réessayer.",
    ), 429


@app.errorhandler(404)
def not_found(e):
    """Rediriger toute URL inconnue vers le portail (captive portal)."""
    return redirect("/", code=302)


# ─────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_ssl_cert()

    ssl_context = (CERT_FILE, KEY_FILE)

    logger.info("Démarrage du portail de setup sur https://%s:%d", PORTAL_HOST, PORTAL_PORT)
    logger.info("Certificat : %s", CERT_FILE)

    app.run(
        host=PORTAL_HOST,
        port=PORTAL_PORT,
        ssl_context=ssl_context,
        debug=False,
        threaded=True,
    )