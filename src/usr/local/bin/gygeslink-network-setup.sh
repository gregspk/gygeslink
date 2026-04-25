#!/bin/bash
# GygesLink — Configuration réseau au boot
# Exécuté par gygeslink-network-setup.service (Type=oneshot)
#
# ARCHITECTURE RÉSEAU :
#   - wlan0 : géré par NetworkManager (WiFi + DHCP + MAC random)
#   - usb0 ou usb1 : configuré ici (USB gadget NCM, côté PC)
#   - iptables : fail-close appliqué ici AVANT que Tor ne démarre
#
# L'interface USB peut s'appeler usb0 ou usb1 selon l'ordre de
# création des gadgets. On la détecte dynamiquement.

set -uo pipefail

LOG() { echo "[gygeslink-network] $*"; }
ERR() { echo "[gygeslink-network] ERREUR: $*" >&2; }

USB_ADDR="192.168.100.1/24"

if [ -f /data/gygeslink/network.conf ]; then
    LOG "Chargement de /data/gygeslink/network.conf..."
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${key// /}" ]] && continue
        key="${key// /}"
        val="${val// /}"
        case "$key" in
            USB_ADDR) USB_ADDR="$val" ;;
        esac
    done < /data/gygeslink/network.conf
fi

# ── Détecter l'interface USB ─────────────────────────────────────
# Le gadget NCM peut créer usb0 ou usb1. On prend la première
# interface dont le nom commence par "usb" et qui a une MAC correspondante.
USB_IF=""
for iface in usb0 usb1 usb2; do
    if ip link show "$iface" > /dev/null 2>&1; then
        USB_IF="$iface"
        break
    fi
done

if [ -z "$USB_IF" ]; then
    LOG "Aucune interface USB détectée — gadget USB non actif. Continue sans."
else
    LOG "Interface USB détectée : $USB_IF"
    ip link set "$USB_IF" up
    ip addr flush dev "$USB_IF" 2>/dev/null || true
    ip addr add "$USB_ADDR" dev "$USB_IF"
    LOG "Interface $USB_IF configurée : $USB_ADDR"
fi

# ── Activer le routage + désactiver IPv6 ──────────────────────────
sysctl -w net.ipv4.ip_forward=1 > /dev/null
sysctl -w net.ipv6.conf.all.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.default.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.lo.disable_ipv6=1 > /dev/null

# ── Lancer dnsmasq (DHCP pour le PC) ─────────────────────────────
if [ -n "$USB_IF" ] && [ -f /etc/dnsmasq.d/gygeslink-usb0.conf ]; then
    killall dnsmasq 2>/dev/null || true
    sleep 1
    # dnsmasq config référence usb0 — remplacer par l'interface réelle
    dnsmasq -u dnsmasq -p 0 \
        -i "$USB_IF" \
        -I lo \
        -F "192.168.100.100,192.168.100.110,255.255.255.0,12h" \
        -O "3,192.168.100.1" \
        -O "6,192.168.100.1" \
        2>/dev/null || true
    LOG "dnsmasq lancé sur $USB_IF — DHCP actif."
fi

# ── Attendre que wlan0 ait une IPv4 ──────────────────────────────
if [ ! -f /data/gygeslink/wifi.conf ]; then
    ERR "Pas de wifi.conf — mode setup."
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
    fi
fi

# ── Adapter les configs iptables et torrc à l'interface USB ─────
# Remplacer usb0 par l'interface réelle dans les règles iptables
# sauf si c'est déjà usb0 (pas de remplacement nécessaire)
if [ -n "$USB_IF" ] && [ "$USB_IF" != "usb0" ]; then
    LOG "Adaptation des règles iptables pour $USB_IF..."
    sed -i "s/usb0/$USB_IF/g" /etc/gygeslink/iptables-drop.rules
    sed -i "s/usb0/$USB_IF/g" /etc/gygeslink/iptables-tor.rules
    sed -i "s/usb0/$USB_IF/g" /etc/tor/torrc
fi

# ── Appliquer iptables fail-close ────────────────────────────────
LOG "Application des règles iptables fail-close..."

if ! iptables-restore < /etc/gygeslink/iptables-drop.rules; then
    ERR "Échec de iptables-restore."
    exit 1
fi

if ! ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules; then
    ERR "Échec de ip6tables-restore."
    exit 1
fi

LOG "iptables fail-close actif."

# ── Mode setup : ouvrir le portail HTTPS si nécessaire ──────────
if [ ! -f /data/gygeslink/setup-done ]; then
    LOG "Mode setup : ouverture portail HTTPS."
    iptables -I INPUT -i "$USB_IF" -p tcp --dport 443 -j ACCEPT
    iptables -I OUTPUT -o "$USB_IF" -p tcp --sport 443 -j ACCEPT
fi

LOG "Configuration réseau terminée."