#!/bin/bash
# GygesLink — Script d'installation
# Exécuté manuellement après le premier boot d'Armbian.
# Configure le WiFi via NetworkManager, déploie les fichiers, active les services.

set -euo pipefail

LOG() { echo "[gygeslink-install] $*"; }
ERR() { echo "[gygeslink-install] ERREUR: $*" >&2; }

# ── Vérifications préalables ──────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    ERR "Ce script doit être exécuté en root (sudo)."
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$REPO_DIR/src/usr/local/bin/gygeslink-network-setup.sh" ]; then
    ERR "Ce script doit être exécuté depuis /opt/gygeslink/install.sh"
    exit 1
fi

# ── Demander le WiFi ─────────────────────────────────────────────
read -p "SSID WiFi : " WIFI_SSID
read -p "Mot de passe WiFi : " WIFI_PSK

if [ -z "$WIFI_SSID" ]; then
    ERR "SSID vide — abandon."
    exit 1
fi

# ── Configurer NetworkManager pour le WiFi ────────────────────────
NM_CONN_FILE="/etc/NetworkManager/system-connections/GygesLink-WiFi.nmconnection"

cat > "$NM_CONN_FILE" << EOF
[connection]
id=GygesLink-WiFi
type=wifi
interface-name=wlan0
autoconnect=true

[wifi]
ssid=$WIFI_SSID
mode=infrastructure

[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PSK

[ipv4]
method=auto

[ipv6]
method=disabled
EOF

chmod 600 "$NM_CONN_FILE"
LOG "Connexion WiFi NM configurée : $WIFI_SSID"

# ── Garder wifi.conf pour le portail setup (premier boot) ─────────
mkdir -p /data/gygeslink
cat > /data/gygeslink/wifi.conf << EOF
network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PSK"
    key_mgmt=WPA-PSK
}
EOF
chmod 600 /data/gygeslink/wifi.conf

# ── Activer l'overlay dwc2 pour le mode USB gadget ──────────────
if [ -f /boot/armbianEnv.txt ]; then
    if grep -q "^overlays=" /boot/armbianEnv.txt; then
        sed -i 's/^overlays=.*/& dwc2/' /boot/armbianEnv.txt
    else
        echo "overlays=dwc2" >> /boot/armbianEnv.txt
    fi
    LOG "Overlay dwc2 ajouté à /boot/armbianEnv.txt"
else
    ERR "/boot/armbianEnv.txt non trouvé — ajouter l'overlay dwc2 manuellement."
fi

# ── Désactiver le service tor de Debian ───────────────────────────
systemctl stop tor 2>/dev/null || true
systemctl disable tor 2>/dev/null || true

# ── Déployer les fichiers du repo ─────────────────────────────────
cp -rv "$REPO_DIR/src/*" /
LOG "Fichiers déployés."

chmod +x /usr/local/bin/gygeslink-*.sh /usr/local/bin/gygeslink-*.py \
         /usr/local/bin/noise_generator.py

# ── Désactiver dnsmasq au boot (lancé manuellement par network-setup) ──
systemctl disable dnsmasq 2>/dev/null || true

# ── Activer les services GygesLink ────────────────────────────────
systemctl enable gygeslink-usb-gadget.service
systemctl enable gygeslink-network-setup.service
systemctl enable gygeslink-tor.service
systemctl enable gygeslink-iptables-open.service
systemctl enable gygeslink-jitter.service
systemctl enable gygeslink-noise.service

# LED et bouton (non câblés en développement)
systemctl disable gygeslink-led.service 2>/dev/null || true
systemctl disable gygeslink-button.service 2>/dev/null || true

# ── Setup-done (WiFi déjà configuré) ──────────────────────────────
touch /data/gygeslink/setup-done

# ── Activer NetworkManager ────────────────────────────────────────
systemctl enable NetworkManager
systemctl start NetworkManager 2>/dev/null || true

LOG "============================================"
LOG "Installation terminée."
LOG "Le Pi va redémarrer dans 5 secondes."
LOG "Après le reboot :"
LOG "  - SSH : ssh gygeslink@<IP_du_Pi>"
LOG "  - USB : brancher le câble USB-C au PC"
LOG "============================================"

sleep 5
reboot