#!/bin/bash
# GygesLink — Jitter temporel via HTB + netem child sur wlan0
# netem directement en root sur wlan0 détruit le qdisc natif du driver WiFi
# → déassociation. HTB en root préserve le driver, netem en enfant ajoute le délai.

LOG_TAG="gygeslink-jitter"
log() { logger -t "$LOG_TAG" "$@"; }
err() { logger -t "$LOG_TAG" "ERROR: $@"; }

WLAN_IF="wlan0"

case "$1" in
    start)
        if ! ip link show "$WLAN_IF" &>/dev/null; then
            err "Interface $WLAN_IF inexistante — jitter ignoré."
            exit 0
        fi

        log "Configuration HTB root + netem child sur $WLAN_IF..."

        tc qdisc del dev "$WLAN_IF" root 2>/dev/null || true

        tc qdisc add dev "$WLAN_IF" root handle 1: htb default 10

        tc class add dev "$WLAN_IF" parent 1: classid 1:10 htb rate 1000mbit

        tc qdisc add dev "$WLAN_IF" parent 1:10 handle 10: netem \
            delay 20ms 15ms distribution pareto

        log "Jitter actif (HTB+netem child sur $WLAN_IF)."
        ;;
    stop)
        log "Retrait du jitter sur $WLAN_IF..."
        tc qdisc del dev "$WLAN_IF" root 2>/dev/null || true
        log "Jitter retiré."
        ;;
    *)
        echo "Usage: $0 {start|stop}" >&2
        exit 1
        ;;
esac