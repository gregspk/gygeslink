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
        # ── Attendre que Tor soit réellement opérationnel ──────────
        # Les règles de redirection pointent vers TransPort 9040 et
        # DNSPort 5353. Si Tor n'écoute pas encore, les paquets du
        # PC sont redirigés vers des ports fermés = connexion refusée.
        # Ce n'est PAS une fuite de trafic (fail-close reste actif),
        # mais c'est un mauvais UX. On attend 180s max.
        log "Attente du bootstrap Tor (port 9040)..."
        MAX_BOOTSTRAP=180
        WAITED=0
        while [ "$WAITED" -lt "$MAX_BOOTSTRAP" ]; do
            if ss -tlnp 2>/dev/null | grep -q ":9040 "; then
                log "Tor TransPort 9040 en écoute — bootstrap OK."
                break
            fi
            sleep 1
            WAITED=$((WAITED + 1))
        done
        if [ "$WAITED" -ge "$MAX_BOOTSTRAP" ]; then
            err "Timeout : Tor n'a pas bootstrappé en ${MAX_BOOTSTRAP}s."
            err "Les règles iptables vont être appliquées malgré tout (trafic = erreur de connexion, pas de leak)."
        fi

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
        conntrack -F 2>/dev/null || log "conntrack -F ignoré (paquet absent, non critique)"
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
    bypass)
        log "Application des règles iptables-bypass (mode pause)..."

        if [ -n "$USB_IF" ] && [ "$USB_IF" != "usb0" ]; then
            sed "s/usb0/$USB_IF/g" /etc/gygeslink/iptables-bypass.rules > /tmp/iptables-bypass-active.rules
        else
            cp /etc/gygeslink/iptables-bypass.rules /tmp/iptables-bypass-active.rules
        fi

        if iptables-restore < /tmp/iptables-bypass-active.rules 2>/tmp/iptables-restore-err.log; then
            log "iptables-restore OK (bypass rules)"
        else
            err "iptables-restore a échoué (bypass):"
            cat /tmp/iptables-restore-err.log | logger -t "$LOG_TAG"
            exit 1
        fi
        ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules 2>/tmp/ip6tables-restore-err.log || {
            err "ip6tables-restore a échoué (bypass ipv6):"
            cat /tmp/ip6tables-restore-err.log | logger -t "$LOG_TAG"
        }

        GW=$(ip route show default | awk '/default/ && /wlan0/ {print $3}')
        if [ -n "$GW" ]; then
            iptables -t nat -A PREROUTING -i "${USB_IF:-usb0}" -p udp --dport 53 -j DNAT --to-destination "$GW"
            log "DNS DNAT vers gateway: $GW"
        else
            err "Aucune gateway default sur wlan0 — DNS du PC ne fonctionnera pas en bypass."
        fi

        conntrack -F 2>/dev/null || log "conntrack -F ignoré (paquet absent, non critique)"
        log "Mode bypass actif — trafic en clair."
        ;;
    *)
        echo "Usage: $0 {open|close|bypass}" >&2
        exit 1
        ;;
esac