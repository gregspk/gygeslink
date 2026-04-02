#!/bin/bash
# GygesLink — Script de configuration réseau au boot
# Exécuté par gygeslink-network.service (Type=oneshot)
#
# ORDRE CRITIQUE DE SÉCURITÉ :
#   1. Configurer usb0 (côté PC — USB gadget g_ether)
#   2. Appliquer iptables DROP immédiatement (fail-close atomique)
#   3. Randomiser MAC wlan0 (avant toute association WiFi)
#   4. Se connecter au réseau WiFi via wpa_supplicant + DHCP

set -uo pipefail

LOG() { echo "[gygeslink-network] $*"; }
ERR() { echo "[gygeslink-network] ERREUR: $*" >&2; }

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 1 : Configurer usb0 (interface côté PC)
# usb0 est créée automatiquement par le kernel via dwc2 + g_ether
# (USB gadget mode — le PC voit une carte réseau USB).
# Le boîtier agit comme un serveur DHCP pour le PC.
# ─────────────────────────────────────────────────────────────────────
LOG "Configuration usb0 (côté PC, USB gadget)..."

ip link set usb0 up
ip addr flush dev usb0 2>/dev/null || true
ip addr add 192.168.100.1/24 dev usb0

# Activer le routage IP (le boîtier doit faire transiter les paquets)
sysctl -w net.ipv4.ip_forward=1 > /dev/null

# Désactiver IPv6 sur toutes les interfaces — prévient tout leak.
# Les règles ip6tables-drop.rules constituent une deuxième barrière,
# mais désactiver IPv6 au niveau kernel est plus sûr.
sysctl -w net.ipv6.conf.all.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.default.disable_ipv6=1 > /dev/null

LOG "usb0 configuré : 192.168.100.1/24"

# Relancer dnsmasq pour qu'il prenne en compte usb0 (DHCP côté PC).
# Le fichier /etc/dnsmasq.d/gygeslink-usb0.conf configure le DHCP
# sur 192.168.100.100-110/24 pour le PC branché en USB-C.
systemctl restart dnsmasq 2>/dev/null || true
LOG "dnsmasq (re)démarré — DHCP actif sur usb0."

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 2 : Appliquer les règles iptables DROP (fail-close)
# CRITIQUE : appliqué ici de façon atomique, immédiatement après
# la configuration de usb0, avant toute autre opération réseau.
# Garantit qu'aucun paquet ne peut traverser le boîtier en clair.
# ─────────────────────────────────────────────────────────────────────
LOG "Application des règles iptables fail-close..."

if ! iptables-restore < /etc/gygeslink/iptables-drop.rules; then
    ERR "Échec de iptables-restore — arrêt."
    exit 1
fi

if ! ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules; then
    ERR "Échec de ip6tables-restore — arrêt."
    exit 1
fi

LOG "Règles fail-close actives — tout trafic bloqué sauf DHCP et loopback."

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 3 : Charger la configuration réseau personnalisée (si présente)
# ─────────────────────────────────────────────────────────────────────
if [ -f /data/gygeslink/network.conf ]; then
    LOG "Chargement de /data/gygeslink/network.conf..."
    # shellcheck source=/dev/null
    source /data/gygeslink/network.conf
fi

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 4 : Randomiser l'adresse MAC de wlan0
# wlan0 est l'interface côté routeur FAI (WiFi client).
# L'adresse MAC est visible par le routeur et potentiellement loguée.
# En la randomisant à chaque boot, on évite le tracking matériel.
# DOIT être fait AVANT que wlan0 s'associe au point d'accès.
# ─────────────────────────────────────────────────────────────────────
LOG "Randomisation MAC wlan0..."

ip link set wlan0 down 2>/dev/null || true
if macchanger -r wlan0 > /dev/null 2>&1; then
    NEW_MAC=$(ip link show wlan0 | awk '/ether/ {print $2}')
    LOG "Nouveau MAC wlan0 : $NEW_MAC"
else
    ERR "macchanger indisponible — MAC non randomisé."
fi

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 5 : Connexion WiFi via wpa_supplicant + DHCP
# wifi.conf contient les credentials WPA2 (SSID + mot de passe).
# Créé par gygeslink-setup-portal.py lors du premier setup.
# Si absent : mode setup requis (pas de connexion internet possible).
# ─────────────────────────────────────────────────────────────────────
WIFI_CONF="/data/gygeslink/wifi.conf"

if [ ! -f "$WIFI_CONF" ]; then
    ERR "Aucune config WiFi ($WIFI_CONF) — setup requis."
    ERR "Le boîtier sera en fail-close jusqu'au setup."
    # On ne sort pas en erreur : le service setup prendra le relais
    exit 0
fi

LOG "Connexion WiFi (wpa_supplicant)..."

# Lancer wpa_supplicant en arrière-plan
# -B = background, -i = interface, -c = config, -P = PID file
if ! wpa_supplicant -B -i wlan0 -c "$WIFI_CONF" -P /run/wpa_supplicant-wlan0.pid 2>/dev/null; then
    ERR "wpa_supplicant a échoué — vérifier $WIFI_CONF."
    exit 0
fi

# Attendre l'association WiFi (max 20s)
WAITED=0
while [ $WAITED -lt 20 ]; do
    if wpa_cli -i wlan0 status 2>/dev/null | grep -q "wpa_state=COMPLETED"; then
        LOG "WiFi associé."
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

if [ $WAITED -ge 20 ]; then
    ERR "Association WiFi timeout (20s) — SSID joignable ?"
    exit 0
fi

# Obtenir une IP via DHCP sur wlan0
LOG "Obtention IP sur wlan0 via DHCP..."

if timeout 30 dhclient -v wlan0 2>/dev/null; then
    WLAN0_IP=$(ip addr show wlan0 | awk '/inet / {print $2}')
    LOG "wlan0 configuré : $WLAN0_IP"
else
    ERR "DHCP wlan0 timeout — routeur joignable ?"
fi

LOG "Configuration réseau terminée."
LOG "État : fail-close actif, Tor pas encore démarré."
