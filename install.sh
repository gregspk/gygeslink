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
    LOG "Configuration du WiFi (mode dev) via netplan"
    read -p "SSID WiFi : " WIFI_SSID
    read -p "Mot de passe WiFi : " WIFI_PSK

    if [ -z "$WIFI_SSID" ]; then
        ERR "SSID vide — abandon."
        exit 1
    fi

    # Netplan yaml pour networkd
    WIFI_SSID_ESCAPED=$(echo "$WIFI_SSID" | sed 's/"/\\"/g')
    WIFI_PSK_ESCAPED=$(echo "$WIFI_PSK" | sed 's/"/\\"/g')
    cat > /etc/netplan/30-wifis-dhcp.yaml << EOF
network:
  version: 2
  renderer: networkd
  wifis:
    wlan0:
      dhcp4: true
      macaddress: shuffle
      access-points:
        "$WIFI_SSID_ESCAPED":
          password: "$WIFI_PSK_ESCAPED"
EOF
    chmod 600 /etc/netplan/30-wifis-dhcp.yaml
    LOG "Connexion WiFi netplan configurée : $WIFI_SSID"

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
    iptables conntrack dnsmasq isc-dhcp-client macchanger wpasupplicant \
    tor obfs4proxy wireguard-tools python3-pip python3-libgpiod \
    i2c-tools git python3-flask python3-flask-limiter python3-requests \
    python3-aiohttp network-manager iw

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

# ── Module IFB pour le jitter (netem sur interface virtuelle) ──────
if ! grep -q "^ifb" /etc/modules 2>/dev/null; then
    echo "ifb" >> /etc/modules
    LOG "Module ifb ajouté à /etc/modules."
fi
modprobe ifb numifbs=1 2>/dev/null || true

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
systemctl enable gygeslink-wireguard.service
systemctl disable gygeslink-led.service 2>/dev/null || true
systemctl disable gygeslink-button.service 2>/dev/null || true

# ── Symlink WireGuard ────────────────────────────────────────────
# wg-quick cherche /etc/wireguard/wg0.conf — le fichier réel est sur
# la partition persistante /data. Le symlink permet à wg-quick de
# le trouver sans copier la clé privée sur le rootfs.
mkdir -p /etc/wireguard
ln -sf /data/gygeslink/wg0.conf /etc/wireguard/wg0.conf
LOG "Symlink WireGuard créé : /etc/wireguard/wg0.conf → /data/gygeslink/wg0.conf"

# ── Nettoyer les anciens fichiers NM WiFi si présents ───────────
rm -f /etc/NetworkManager/system-connections/GygesLink-WiFi.nmconnection 2>/dev/null || true

LOG "============================================"
LOG "Installation terminée."
LOG "Le Pi va s'éteindre."
LOG "Débranchez et rebranchez le câble USB-C sur votre PC."
LOG "Le portail setup s'ouvrira automatiquement."
LOG "============================================"

sleep 5
poweroff