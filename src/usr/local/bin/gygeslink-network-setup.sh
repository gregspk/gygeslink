#!/bin/bash
# GygesLink — Configuration réseau au boot
# Exécuté par gygeslink-network-setup.service (Type=oneshot)
#
# ARCHITECTURE RÉSEAU :
#   - wlan0 : géré par NetworkManager (WiFi + DHCP + MAC random)
#     On NE lance PAS wpa_supplicant/dhclient en parallèle de NM.
#     NM est configuré via gygeslink-unmanaged.conf pour ignorer usb0.
#   - usb0 : configuré ici (USB gadget RNDIS, côté PC)
#   - iptables : fail-close appliqué ici AVANT que Tor ne démarre
#
# SÉCURITÉ :
#   - MAC randomisation : configurée dans NM (voir 80-wifi-randmac.conf)
#   - Fail-close iptables : appliqué ici, atomique
#   - Pas de DNS leak : iptables redirige tout UDP/53 vers Tor DNSPort
#   - WiFi credentials : dans /data/gygeslink/wifi.conf (NM les lit)

set -uo pipefail

LOG() { echo "[gygeslink-network] $*"; }
ERR() { echo "[gygeslink-network] ERREUR: $*" >&2; }

USB0_ADDR="192.168.100.1/24"

if [ -f /data/gygeslink/network.conf ]; then
    LOG "Chargement de /data/gygeslink/network.conf..."
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${key// /}" ]] && continue
        key="${key// /}"
        val="${val// /}"
        case "$key" in
            USB0_ADDR) USB0_ADDR="$val" ;;
        esac
    done < /data/gygeslink/network.conf
fi

# ── ÉTAPE 1 : Configurer usb0 (si disponible) ─────────────────────
if ip link show usb0 > /dev/null 2>&1; then
    LOG "Configuration usb0 (côté PC, USB gadget)..."
    ip link set usb0 up
    ip addr flush dev usb0 2>/dev/null || true
    ip addr add "$USB0_ADDR" dev usb0
    LOG "usb0 configuré : $USB0_ADDR"
else
    LOG "usb0 non disponible — gadget USB non actif."
fi

# ── Activer le routage + désactiver IPv6 ──────────────────────────
sysctl -w net.ipv4.ip_forward=1 > /dev/null
sysctl -w net.ipv6.conf.all.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.default.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.lo.disable_ipv6=1 > /dev/null

# ── ÉTAPE 2 : Lancer dnsmasq sur usb0 ─────────────────────────────
if [ -f /etc/dnsmasq.d/gygeslink-usb0.conf ]; then
    killall dnsmasq 2>/dev/null || true
    sleep 1
    dnsmasq -u dnsmasq 2>/dev/null || true
    LOG "dnsmasq lancé — DHCP actif sur usb0."
fi

# ── ÉTAPE 3 : Attendre que wlan0 ait une IPv4 ─────────────────────
# NetworkManager gère wlan0. On attend juste qu'il obtienne une IP.
# Si le WiFi n'est pas configuré (setup-done absent), on skip.
if [ ! -f /data/gygeslink/wifi.conf ]; then
    ERR "Pas de wifi.conf — mode setup, WiFi non configuré."
    ERR "Le portail setup sera accessible via usb0 uniquement."
else
    LOG "Attente IPv4 sur wlan0 (NetworkManager)..."
    WAITED=0
    while [ "$WAITED" -lt 30 ]; do
        if ip addr show wlan0 2>/dev/null | grep -q "inet "; then
            WLAN0_IP=$(ip addr show wlan0 | awk '/inet / {print $2}')
            LOG "wlan0 connecté : $WLAN0_IP"
            break
        fi
        sleep 1
        WAITED=$((WAITED + 1))
    done
    if [ "$WAITED" -ge 30 ]; then
        ERR "wlan0 n'a pas obtenu d'IPv4 en 30s."
        ERR "Vérifier que NetworkManager est actif et le WiFi configuré."
    fi
fi

# ── ÉTAPE 4 : Appliquer iptables fail-close ───────────────────────
LOG "Application des règles iptables fail-close..."

if ! iptables-restore < /etc/gygeslink/iptables-drop.rules; then
    ERR "Échec de iptables-restore."
    exit 1
fi

if ! ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules; then
    ERR "Échec de ip6tables-restore."
    exit 1
fi

LOG "iptables fail-close actif — tout trafic bloqué sauf exceptions."

# ── Mode setup : ouvrir le portail HTTPS sur usb0 ─────────────────
if [ ! -f /data/gygeslink/setup-done ]; then
    LOG "Mode setup : ouverture portail HTTPS sur usb0."
    iptables -I INPUT -i usb0 -p tcp --dport 443 -j ACCEPT
    iptables -I OUTPUT -o usb0 -p tcp --sport 443 -j ACCEPT
fi

LOG "Configuration réseau terminée."