#!/usr/bin/env python3
"""
GygesLink — Captive portal de setup

Serveur HTTPS Flask pour la configuration initiale du boîtier.
Actif uniquement au premier boot (absence de /data/gygeslink/setup-done).

Flux :
  1. L'utilisateur se connecte au WiFi "GygesLink-Setup"
  2. Le navigateur détecte le portail (réponses aux sondes captive portal)
  3. L'utilisateur choisit Classic ou Advanced
  4. Si Advanced : saisie du compte Mullvad (16 chiffres)
  5. Le portail valide, génère la config WireGuard, reboot

Sécurité :
  - HTTPS avec certificat auto-signé (généré au premier lancement)
  - Validation stricte du format du compte avant tout appel API
  - Rate limiting : 5 tentatives POST par minute par IP
  - Appel API Mullvad avec vérification TLS (verify=True)
  - Le port 80 est redirigé vers 443 par iptables (voir gygeslink-setup.service)
"""

import ipaddress
import logging
import os
import re
import subprocess
import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
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
WG_CONF_FILE     = DATA_DIR / "wg0.conf"
WG_EXPIRY_FILE   = DATA_DIR / "wg-expiry.txt"
WIFI_CONF_FILE   = DATA_DIR / "wifi.conf"
BRIDGES_CONF_FILE = DATA_DIR / "bridges.conf"

# NOTE : endpoint à vérifier avec la documentation officielle Mullvad.
# L'API publique Mullvad a évolué. Les endpoints possibles incluent :
#   https://api.mullvad.net/wg/          (ancien, peut être obsolète)
#   https://api.mullvad.net/app/v1/      (v1 API app)
#   https://api.mullvad.net/accounts/v1/ (accounts API)
# Voir : https://mullvad.net/en/help/api/ (ou /fr/help/api/)
# Pour le setup Tier 2 sur le Orange Pi, il faut confirmer l'endpoint exact
# et le format d'authentification (Bearer token, account number, etc.)
# avant déploiement en production.
MULLVAD_API_BASE = "https://api.mullvad.net"
MULLVAD_API_WG   = f"{MULLVAD_API_BASE}/wg/"

# Format attendu : exactement 16 chiffres
ACCOUNT_PATTERN  = re.compile(r"^\d{16}$")

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
    .spinner {
      display: none;
      text-align: center;
      color: #888;
      margin-top: 16px;
      font-size: 14px;
    }
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

  <h2>Choisissez votre mode de protection</h2>

  <form method="post" action="/setup">
    <input type="hidden" name="tier" value="1">
    <button type="submit" class="tier-btn">
      <div class="tier-name">Classic</div>
      <div class="tier-desc">Tor + obfs4 + jitter + bruit de fond</div>
    </button>
  </form>

  <button class="tier-btn" onclick="document.getElementById('advanced-form').style.display='block';this.style.display='none';">
    <div class="tier-name">Advanced</div>
    <div class="tier-desc">WireGuard Mullvad + Tor + obfs4 + jitter + bruit de fond</div>
  </button>

  <form id="advanced-form" method="post" action="/setup"
        style="display:none; margin-top:16px; padding-top:16px; border-top:1px solid #333;">
    <input type="hidden" name="tier" value="2">
    <div class="form-group">
      <label for="account">Numéro de compte Mullvad</label>
      <input type="password" id="account" name="account"
             placeholder="1234567890123456"
             maxlength="16" pattern="\\d{16}" autocomplete="off" required>
      <div class="hint">16 chiffres — disponible sur votre tableau de bord Mullvad</div>
    </div>
    <button type="submit" class="btn-primary"
            onclick="this.textContent='Configuration en cours…';document.querySelector('.spinner').style.display='block'">
      Configurer
    </button>
    <div class="spinner">Génération de la clé WireGuard et appel API Mullvad…<br>Merci de patienter.</div>
    <button type="button" class="btn-secondary"
            onclick="document.getElementById('advanced-form').style.display='none';document.querySelector('.tier-btn:last-of-type').style.display='block'">
      ← Retour
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
    La LED deviendra <strong style="color:#00ff88">verte</strong> une fois la protection active.<br><br>
    Le câble USB-C peut rester branché — la protection est active dès le redémarrage.
  </p>
"""
)

HTML_PROCESSING = HTML_BASE.replace(
    "{% block content %}{% endblock %}",
    """
  <div class="alert alert-info">Configuration en cours…</div>
  <p style="color:#888; font-size:14px;">
    Génération de la clé WireGuard et contact de l'API Mullvad.<br>
    Cette opération peut prendre jusqu'à 30 secondes.
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
# Génération des clés WireGuard
# ─────────────────────────────────────────────────────────────────────

def generate_wireguard_keys() -> tuple[str, str]:
    """
    Génère une paire de clés WireGuard (privée + publique).
    Utilise les outils wg installés sur le Pi.
    Retourne (private_key_b64, public_key_b64).
    """
    # Générer la clé privée
    private_result = subprocess.run(
        ["wg", "genkey"],
        capture_output=True, text=True, check=True,
    )
    private_key = private_result.stdout.strip()

    # Dériver la clé publique depuis la clé privée
    public_result = subprocess.run(
        ["wg", "pubkey"],
        input=private_key,
        capture_output=True, text=True, check=True,
    )
    public_key = public_result.stdout.strip()

    return private_key, public_key


# ─────────────────────────────────────────────────────────────────────
# Appel API Mullvad
# ─────────────────────────────────────────────────────────────────────

def register_wireguard_key(account: str, public_key: str) -> dict:
    """
    Enregistre la clé publique WireGuard auprès de Mullvad.
    Retourne les informations de configuration du serveur.

    NOTE : l'endpoint Mullvad doit être vérifié avant le premier déploiement.
    Si l'API retourne une erreur inattendue, consulter :
      https://mullvad.net/en/help/api/
    """
    # Format attendu pour Mullvad : account number (16 chiffres) utilisé
    # comme identifiant. Certains endpoints nécessitent un header
    # Authorization: Token <account_number> au lieu d'un POST form-data.
    # Le code ci-dessous est paramétrable : si l'endpoint évolue,
    # modifier MULLVAD_API_WG et la structure de la requête.
    try:
        response = requests.post(
            MULLVAD_API_WG,
            data={
                "account": account,
                "pubkey": public_key,
            },
            timeout=30,
            verify=True,
        )
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f"Erreur TLS API Mullvad : {e}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Impossible de joindre l'API Mullvad ({MULLVAD_API_WG}) : {e}")

    if response.status_code == 400:
        raise ValueError("Compte Mullvad invalide ou expiré.")
    if response.status_code == 401:
        raise ValueError("Authentification API Mullvad refusée.")
    if response.status_code == 429:
        raise ValueError("Trop de clés enregistrées. Supprimez des appareils sur votre compte Mullvad.")
    if response.status_code == 404:
        # L'endpoint n'existe probablement plus — nécessite une mise à jour
        raise RuntimeError(
            f"Endpoint API introuvable (404) : {MULLVAD_API_WG}. "
            "L'API Mullvad a peut-être changé. Vérifier la documentation officielle."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Erreur API Mullvad (HTTP {response.status_code}) : {response.text[:200]}"
        )

    return response.json()


def build_wg_config(private_key: str, api_data: dict) -> tuple[str, datetime]:
    """
    Construit le fichier wg0.conf depuis les données retournées par Mullvad.
    Retourne (config_string, expiry_datetime).

    Format attendu de api_data (vérifier avec l'API Mullvad réelle) :
    {
        "ip": "10.x.x.x",
        "server": {
            "hostname": "se-got-wg-001",
            "public_key": "...",
            "ipv4_addr_in": "1.2.3.4"
        },
        "expiry": "2026-12-31T00:00:00+00:00"
    }
    """
    try:
        server_pubkey = api_data["peers"][0]["public_key"]
    except (KeyError, IndexError, TypeError):
        server_pubkey = api_data.get("server", {}).get("public_key", "")
    if not server_pubkey:
        raise ValueError("Clé publique serveur Mullvad absente dans la réponse API.")
    server_ip     = api_data.get("server", {}).get("ipv4_addr_in", "")
    assigned_ip   = api_data.get("ip", "")
    expiry_str    = api_data.get("expiry", "")

    # Valider les trois champs injectés dans wg0.conf — confiance zéro sur
    # la réponse API (MITM, API Mullvad compromise, réponse malformée).

    # assigned_ip : IP de l'interface tunnel côté client
    try:
        ipaddress.ip_address(assigned_ip)
    except ValueError:
        raise ValueError(f"Adresse IP invalide retournée par l'API Mullvad (assigned_ip) : {assigned_ip!r}")

    # server_ip : IP de l'endpoint Mullvad (Endpoint = server_ip:51820)
    try:
        ipaddress.ip_address(server_ip)
    except ValueError:
        raise ValueError(f"Adresse IP invalide retournée par l'API Mullvad (server_ip) : {server_ip!r}")

    # server_pubkey : clé publique Curve25519 encodée en base64
    # Format WireGuard : 32 octets → 44 caractères base64 (dont 1 '=' de padding)
    WG_PUBKEY_RE = re.compile(r'^[A-Za-z0-9+/]{43}=$')
    if not WG_PUBKEY_RE.match(server_pubkey):
        raise ValueError(f"Clé publique serveur invalide retournée par l'API Mullvad : {server_pubkey!r}")

    config = f"""[Interface]
PrivateKey = {private_key}
Address = {assigned_ip}/32
DNS = 193.138.218.74

[Peer]
PublicKey = {server_pubkey}
AllowedIPs = 0.0.0.0/0
Endpoint = {server_ip}:51820
PersistentKeepalive = 25
"""

    expiry = None
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
        except ValueError:
            pass

    if not expiry:
        # Valeur de fallback : 30 jours
        expiry = datetime.now(timezone.utc) + timedelta(days=30)

    return config, expiry


# ─────────────────────────────────────────────────────────────────────
# Routes Flask
# ─────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """Page d'accueil du portail.
    Flux : WiFi → Bridges obfs4 → Choix du tier.
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
    """Enregistre les credentials WiFi dans /data/gygeslink/wifi.conf."""
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
    # Rejeter les caractères de contrôle — prévient l'injection de directives
    # wpa_supplicant via un retour à la ligne dans le SSID ou le mot de passe.
    if re.search(r'[\r\n\x00]', ssid) or re.search(r'[\r\n\x00]', password):
        return render_template_string(HTML_WIFI, error="Caractères invalides dans le SSID ou le mot de passe.")

    # Écrire la config wpa_supplicant
    # Les guillemets dans SSID/password sont échappés pour éviter l'injection
    ssid_escaped     = ssid.replace('"', '\\"')
    password_escaped = password.replace('"', '\\"')

    wifi_config = f"""ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1

network={{
    ssid="{ssid_escaped}"
    psk="{password_escaped}"
    key_mgmt=WPA-PSK
}}
"""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WIFI_CONF_FILE.write_text(wifi_config)
    WIFI_CONF_FILE.chmod(0o600)   # Credentials — lecture seule pour root

    logger.info("WiFi configuré : SSID=%r", ssid)

    existing = ""
    return render_template_string(HTML_BRIDGES, error=None, info=None, existing_bridges=existing)


@app.route("/bridges", methods=["POST"])
@limiter.limit("10 per minute")
def bridges():
    """Enregistre les bridges obfs4 dans /data/gygeslink/bridges.conf."""
    if request.form.get("skip"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BRIDGES_CONF_FILE.touch()
        BRIDGES_CONF_FILE.chmod(0o644)
        logger.info("Bridges obfs4 : ignoré par l'utilisateur.")
        return render_template_string(HTML_HOME, error=None)

    raw = request.form.get("bridges", "").strip()

    if not raw:
        return render_template_string(HTML_BRIDGES, error="Collez au moins une ligne de bridge obfs4.", info=None, existing_bridges="")

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    valid = []
    for line in lines:
        # Retirer le préfixe "Bridge " si l'utilisateur a copié la ligne complète
        if line.lower().startswith("bridge "):
            line = line[7:]
        # Validation basique : doit commencer par obfs4 + IP:PORT
        if not re.match(r'^obfs4\s+\d{1,3}(\.\d{1,3}){3}:\d{1,5}\s+\S+', line):
            continue
        # Rejeter les caractères de contrôle
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
    - Tier 1 (Classic) : écrit setup-done et reboot.
    - Tier 2 (Advanced) : valide le compte, génère WireGuard, reboot.
    """
    tier = request.form.get("tier", "1")

    # ── Tier 1 : Classic ─────────────────────────────────────────────
    if tier == "1":
        logger.info("Setup Classic (Tier 1).")
        _finalize_setup()
        return render_template_string(
            HTML_SUCCESS,
            message="Mode Classic configuré. Redémarrage en cours…",
        )

    # ── Tier 2 : Advanced ────────────────────────────────────────────
    account = request.form.get("account", "").strip()

    # Validation du format avant tout appel réseau
    if not ACCOUNT_PATTERN.match(account):
        logger.warning("Format de compte invalide : %r", account[:4] + "****")
        return render_template_string(
            HTML_HOME,
            error="Format invalide. Le numéro de compte Mullvad doit contenir exactement 16 chiffres.",
        )

    logger.info("Setup Advanced (Tier 2) — génération des clés WireGuard...")

    try:
        # Générer les clés WireGuard localement
        private_key, public_key = generate_wireguard_keys()

        # Enregistrer la clé publique auprès de Mullvad
        logger.info("Appel API Mullvad...")
        api_data = register_wireguard_key(account, public_key)

        # Construire la config WireGuard
        wg_config, expiry = build_wg_config(private_key, api_data)

        # Écrire les fichiers dans /data (partition persistante)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WG_CONF_FILE.write_text(wg_config)
        WG_CONF_FILE.chmod(0o600)   # Clé privée — lecture seule pour root

        # Sauvegarder la date d'expiration pour alerter l'utilisateur
        WG_EXPIRY_FILE.write_text(expiry.isoformat())

        logger.info("Config WireGuard écrite. Expiration : %s", expiry.isoformat())

        _finalize_setup()
        return render_template_string(
            HTML_SUCCESS,
            message=f"Mode Advanced configuré (Mullvad actif jusqu'au {expiry.strftime('%d/%m/%Y')}). Redémarrage en cours…",
        )

    except ValueError as e:
        logger.warning("Erreur configuration Advanced : %s", e)
        return render_template_string(HTML_HOME, error=str(e))

    except subprocess.CalledProcessError:
        logger.error("Erreur génération clés WireGuard (wg non disponible ?)")
        return render_template_string(
            HTML_HOME,
            error="Erreur interne : impossible de générer les clés WireGuard.",
        )

    except requests.exceptions.SSLError:
        logger.error("Erreur TLS lors de l'appel API Mullvad.")
        return render_template_string(
            HTML_HOME,
            error="Erreur de sécurité lors de la connexion à l'API Mullvad. Réessayez.",
        )

    except requests.exceptions.RequestException as e:
        logger.error("Erreur réseau API Mullvad : %s", e)
        return render_template_string(
            HTML_HOME,
            error="Impossible de joindre l'API Mullvad. Vérifiez que le boîtier a accès à internet via WiFi (wlan0).",
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

    # Reboot différé de 3 secondes (non-bloquant pour que Flask renvoie sa réponse)
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
