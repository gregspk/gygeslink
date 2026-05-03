#!/bin/bash
# GygesLink — Appliquer les règles iptables de redirection Tor
# Exécuté par gygeslink-iptables-open.service
# Détecte l'interface USB réelle (usb0 ou usb1) et adapte les règles.

set -euo pipefail

# Détecter l'interface USB
USB_IF=""
for iface in usb0 usb1 usb2; do
    if ip link show "$iface" 2>/dev/null | grep -q "UP"; then
        USB_IF="$iface"
        break
    fi
done

# Si l'interface USB n'est pas usb0, préparer les règles adaptées
if [ -n "$USB_IF" ] && [ "$USB_IF" != "usb0" ]; then
    sed "s/usb0/$USB_IF/g" /etc/gygeslink/iptables-tor.rules > /tmp/iptables-tor-active.rules
    sed "s/usb0/$USB_IF/g" /etc/gygeslink/iptables-drop.rules > /tmp/iptables-drop-active.rules
else
    cp /etc/gygeslink/iptables-tor.rules /tmp/iptables-tor-active.rules
    cp /etc/gygeslink/iptables-drop.rules /tmp/iptables-drop-active.rules
fi

case "$1" in
    open)
        iptables-restore < /tmp/iptables-tor-active.rules
        ip6tables-restore < /etc/gygeslink/ip6tables-tor.rules
        ;;
    close)
        iptables-restore < /tmp/iptables-drop-active.rules
        ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules
        ;;
    *)
        echo "Usage: $0 {open|close}" >&2
        exit 1
        ;;
esac