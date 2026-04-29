#!/bin/bash
# GygesLink — Script d'installation
# Exécuté sur le Pi après le premier boot d'Armbian.
# Configure le WiFi, déploie les fichiers, crée les utilisateurs, active les services.
#
# Modes :
#   ./install.sh          — mode dev : configure WiFi + marque setup-done
#   ./install.sh factory   — mode factory : pas de WiFi, pas de setup-done
#                           (pour image à flasher — le portail setup s'activera au boot)

set -euo pipefail

LOG() { echo "[gygeslink-install] $*"; }
ERR() { echo "[gygeslink-install] ERREUR: $*" >&2; }

REPO_DIR="/opt/gygeslink"
MODE="${1:-dev}"

if [ "$(id -u)" -ne 0 ]; then
    ERR "Ce script doit être exécuté en root (sudo)."
    exit 1
fi

if [ ! -f "$REPO_DIR/src/usr/local/bin/gygeslink-network-setup.sh" ]; then
    ERR "Ce script doit être exécuté depuis $REPO_DIR/install.sh"
    exit 1
fi

# ── WiFi (mode dev uniquement) ────────────────────────────────────
if [ "$MODE" = "dev" ]; then
    LOG "Configuration du WiFi (mode dev)"
    read -p "SSID WiFi : " WIFI_SSID
    read -p "Mot de passe WiFi : " WIFI_PSK

    if [ -z "$WIFI_SSID" ]; then
        ERR "SSID vide — abandon."
        exit 1
    fi

    mkdir -p /etc/NetworkManager/system-connections

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

    mkdir -p /data/gygeslink
    cat > /data/gygeslink/wifi.conf << EOF
network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PSK"
    key_mgmt=WPA-PSK
}
EOF
    chmod 600 /data/gygeslink/wifi.conf
else
    LOG "Mode factory — WiFi configuré via le portail setup au premier boot."
fi

# ── Overlay dwc2 pour USB gadget ────────────────────────────────
if [ -f /boot/armbianEnv.txt ]; then
    if grep -q "^overlays=" /boot/armbianEnv.txt; then
        if ! grep -q "dwc2" /boot/armbianEnv.txt; then
            sed -i 's/^overlays=.*/& dwc2/' /boot/armbianEnv.txt
        fi
    else
        echo "overlays=dwc2" >> /boot/armbianEnv.txt
    fi
    LOG "Overlay dwc2 configuré"
else
    ERR "/boot/armbianEnv.txt non trouvé — ajout manuel requis."
fi

# ── Installer les paquets ───────────────────────────────────────
LOG "Installation des paquets..."
apt update && apt install -y \
    iptables dnsmasq isc-dhcp-client macchanger wpasupplicant \
    tor obfs4proxy wireguard-tools python3-pip python3-libgpiod \
    i2c-tools git python3-flask python3-flask-limiter python3-requests \
    python3-aiohttp network-manager

# ── Installer aiohttp-socks (absent des dépôts Debian) ───────────
pip3 install --break-system-packages aiohttp-socks 2>/dev/null || true

# ── Créer les utilisateurs système ──────────────────────────────
id gygeslink-noise &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin gygeslink-noise
LOG "Utilisateur gygeslink-noise créé"

# ── Désactiver le service tor de Debian ─────────────────────────
systemctl stop tor 2>/dev/null || true
systemctl disable tor 2>/dev/null || true

# ── Déployer les fichiers ────────────────────────────────────────
LOG "Déploiement des fichiers..."
cp -r "$REPO_DIR/src/etc" / 2>/dev/null || true
cp -r "$REPO_DIR/src/usr" / 2>/dev/null || true
cp -r "$REPO_DIR/src/data" / 2>/dev/null || true
chmod +x /usr/local/bin/gygeslink-*.sh /usr/local/bin/gygeslink-*.py \
         /usr/local/bin/noise_generator.py 2>/dev/null || true

# ── Setup-done ──────────────────────────────────────────────────
if [ "$MODE" = "dev" ]; then
    touch /data/gygeslink/setup-done
    LOG "setup-done créé (mode dev — skip portail)."
else
    rm -f /data/gygeslink/setup-done 2>/dev/null || true
    LOG "setup-done absent (mode factory — portail actif au premier boot)."
fi

# ── NetworkManager ──────────────────────────────────────────────
systemctl enable NetworkManager
systemctl start NetworkManager 2>/dev/null || true

# ── Désactiver dnsmasq au boot ──────────────────────────────────
systemctl disable dnsmasq 2>/dev/null || true

# ── Activer les services GygesLink ──────────────────────────────
systemctl daemon-reload
systemctl enable gygeslink-usb-gadget.service
systemctl enable gygeslink-network-setup.service
systemctl enable gygeslink-setup.service
systemctl enable gygeslink-tor.service
systemctl enable gygeslink-iptables-open.service
systemctl enable gygeslink-jitter.service
systemctl enable gygeslink-noise.service
systemctl enable gygeslink-api.service
systemctl disable gygeslink-led.service 2>/dev/null || true
systemctl disable gygeslink-button.service 2>/dev/null || true

# ── Supprimer les connexions WiFi NM de l'install Armbian ────────
nmcli -t -f TYPE,NAME connection show 2>/dev/null | grep '802-11-wireless' | cut -d: -f2- | while read -r name; do
    nmcli connection delete "$name" 2>/dev/null || true
done
LOG "Connexions WiFi Armbian supprimées."

LOG "============================================"
LOG "Installation terminée."
LOG "Le Pi va s'éteindre."
LOG "Débranchez et rebranchez le câble USB-C sur votre PC."
LOG "Le portail setup s'ouvrira automatiquement."
LOG "============================================"

sleep 5
poweroff