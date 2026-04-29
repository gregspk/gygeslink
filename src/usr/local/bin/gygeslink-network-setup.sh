#!/bin/bash
# GygesLink — Configuration réseau au boot
# Exécuté par gygeslink-network-setup.service (Type=oneshot)
#
# ARCHITECTURE RÉSEAU :
#   - wlan0 : géré par NetworkManager (WiFi + DHCP + MAC random)
#   - usb0/usb1 : configuré ici (USB gadget NCM, côté PC)
#   - iptables : fail-close appliqué ici AVANT que Tor ne démarre

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

# ── Désactiver systemd-resolved sur le port 53 ───────────────────
# systemd-resolved écoute sur 127.0.0.53:53 par défaut, ce qui
# empêche dnsmasq de démarrer. On le configure pour ne plus lier
# le port 53, seulement le stub resolver.
if [ -f /etc/systemd/resolved.conf ]; then
    if ! grep -q "^DNSStubListener=no" /etc/systemd/resolved.conf; then
        sed -i 's/^#DNSStubListener=.*/DNSStubListener=no/' /etc/systemd/resolved.conf
        grep -q "^DNSStubListener=no" /etc/systemd/resolved.conf || \
            echo "DNSStubListener=no" >> /etc/systemd/resolved.conf
        systemctl restart systemd-resolved 2>/dev/null || true
        LOG "systemd-resolved port 53 désactivé."
    fi
fi

# ── Détecter l'interface USB ─────────────────────────────────────
USB_IF=""
for iface in usb0 usb1 usb2; do
    if ip link show "$iface" > /dev/null 2>&1; then
        USB_IF="$iface"
        break
    fi
done

if [ -z "$USB_IF" ]; then
    LOG "Aucune interface USB détectée — gadget USB non actif."
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
if [ -n "$USB_IF" ]; then
    killall dnsmasq 2>/dev/null || true
    sleep 1
    if [ -f /etc/dnsmasq.d/gygeslink-usb0.conf ]; then
        if [ -f /data/gygeslink/setup-done ]; then
            dnsmasq -u dnsmasq -p 0 \
                -i "$USB_IF" \
                -I lo \
                -F "192.168.100.100,192.168.100.110,255.255.255.0,12h" \
                -O "3,192.168.100.1" \
                -O "6,192.168.100.1" \
                2>/dev/null || true
            LOG "dnsmasq lancé sur $USB_IF — DHCP avec gateway + DNS (Tor en service)."
        else
            dnsmasq -u dnsmasq -p 0 \
                -i "$USB_IF" \
                -I lo \
                -F "192.168.100.100,192.168.100.110,255.255.255.0,12h" \
                2>/dev/null || true
            LOG "dnsmasq lancé sur $USB_IF — DHCP SANS gateway ni DNS (mode setup, PC garde son WiFi)."
        fi
    fi
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

# ── Appliquer iptables fail-close ────────────────────────────────
# Les règles iptables utilisent "usb0" par défaut. Si l'interface
# est différente, on injecte des règles supplémentaires plutôt que
# de modifier les fichiers (plus sûr avec overlayfs).

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

# ── Si l'interface USB n'est pas usb0, ajouter les règles manquantes ─
if [ -n "$USB_IF" ] && [ "$USB_IF" != "usb0" ]; then
    LOG "Ajout des règles iptables pour $USB_IF..."
    # DHCP serveur sur l'interface USB réelle
    iptables -I INPUT -i "$USB_IF" -p udp --dport 67 -j ACCEPT
    iptables -I OUTPUT -o "$USB_IF" -p udp --sport 67 -j ACCEPT
    # Redirection DNS et TCP vers Tor (comme dans iptables-tor.rules)
    # Ces règles seront remplacées quand iptables-open s'activera
fi

# ── Mode setup : ouvrir le portail HTTPS si nécessaire ──────────
if [ ! -f /data/gygeslink/setup-done ]; then
    if [ -n "$USB_IF" ]; then
        LOG "Mode setup : ouverture portail HTTPS."
        iptables -I INPUT -i "$USB_IF" -p tcp --dport 443 -j ACCEPT
        iptables -I OUTPUT -o "$USB_IF" -p tcp --sport 443 -j ACCEPT
    else
        ERR "Mode setup mais pas d'interface USB — portail inaccessible."
    fi
fi

LOG "Configuration réseau terminée."