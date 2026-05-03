#!/bin/bash
# GygesLink — Appliquer les règles iptables de redirection Tor
# Exécuté par gygeslink-iptables-open.service

LOG_TAG="gygeslink-iptables"

log() { logger -t "$LOG_TAG" "$@"; }
err() { logger -t "$LOG_TAG" "ERROR: $@"; }

# Détecter l'interface USB
USB_IF=""
for iface in usb0 usb1 usb2; do
    if ip link show "$iface" 2>/dev/null | grep -q "UP"; then
        USB_IF="$iface"
        break
    fi
done

log "Interface USB détectée: ${USB_IF:-aucune}"

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
        log "Application des règles iptables-tor..."
        if iptables-restore < /tmp/iptables-tor-active.rules 2>/tmp/iptables-restore-err.log; then
            log "iptables-restore OK (tor rules)"
        else
            err "iptables-restore a échoué:"
            cat /tmp/iptables-restore-err.log | logger -t "$LOG_TAG"
            exit 1
        fi
        if ip6tables-restore < /etc/gygeslink/ip6tables-tor.rules 2>/tmp/ip6tables-restore-err.log; then
            log "ip6tables-restore OK (tor rules)"
        else
            err "ip6tables-restore a échoué:"
            cat /tmp/ip6tables-restore-err.log | logger -t "$LOG_TAG"
            exit 1
        fi
        ;;
    close)
        log "Application des règles iptables-drop (fail-close)..."
        iptables-restore < /tmp/iptables-drop-active.rules 2>/tmp/iptables-restore-err.log || {
            err "iptables-restore a échoué (close):"
            cat /tmp/iptables-restore-err.log | logger -t "$LOG_TAG"
        }
        ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules 2>/tmp/ip6tables-restore-err.log || {
            err "ip6tables-restore a échoué (close):"
            cat /tmp/ip6tables-restore-err.log | logger -t "$LOG_TAG"
        }
        ;;
    *)
        echo "Usage: $0 {open|close}" >&2
        exit 1
        ;;
esac