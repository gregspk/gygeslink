#!/bin/bash
# GygesLink — Jitter temporel via IFB + netem
#
# IFB (Intermediate Functional Block) est une interface virtuelle kernel
# qui reçoit le trafic sortant de wlan0 via tc mirred redirect.
# netem est appliqué sur ifb0, PAS sur wlan0 — le driver WiFi reste
# totalement intact. Le trafic de contrôle (DHCP, ARP, EAPOL) passe
# directement par wlan0 sans délai.
#
# Flot de paquets :
#   Paquet sortant → wlan0 (qdisc natif) → ingress mirred → ifb0 (netem) → wlan0 TX

LOG_TAG="gygeslink-jitter"
log() { logger -t "$LOG_TAG" "$@"; }
err() { logger -t "$LOG_TAG" "ERROR: $@"; }

WLAN_IF="wlan0"
IFB_IF="ifb0"
PARETO_DIST="/usr/share/tc/pareto.dist"

case "$1" in
    start)
        if ! ip link show "$WLAN_IF" &>/dev/null; then
            err "Interface $WLAN_IF inexistante — jitter ignoré."
            exit 0
        fi

        if ! modprobe ifb numifbs=1 2>/dev/null; then
            err "Module ifb indisponible — jitter ignoré (WiFi reste stable)."
            exit 0
        fi

        if ! ip link set dev "$IFB_IF" up 2>/dev/null; then
            err "Impossible d'activer $IFB_IF — jitter ignoré."
            exit 0
        fi

        log "Configuration IFB + netem..."

        tc qdisc del dev "$WLAN_IF" root 2>/dev/null || true
        tc qdisc del dev "$WLAN_IF" ingress 2>/dev/null || true
        tc qdisc del dev "$IFB_IF" root 2>/dev/null || true

        tc qdisc add dev "$WLAN_IF" handle ffff: ingress

        tc filter add dev "$WLAN_IF" parent ffff: protocol ip \
            u32 match u32 0 0 \
            action mirred egress redirect dev "$IFB_IF"

        if [ -f "$PARETO_DIST" ]; then
            tc qdisc add dev "$IFB_IF" root netem \
                delay 20ms 15ms distribution pareto
            log "Jitter actif (IFB+netem pareto sur $IFB_IF)."
        else
            tc qdisc add dev "$IFB_IF" root netem \
                delay 20ms 15ms
            log "Jitter actif (IFB+netem sur $IFB_IF, pareto.dist absent — jitter uniforme)."
        fi
        ;;

    stop)
        log "Retrait du jitter..."

        tc qdisc del dev "$WLAN_IF" ingress 2>/dev/null || true
        tc qdisc del dev "$IFB_IF" root 2>/dev/null || true
        ip link set dev "$IFB_IF" down 2>/dev/null || true
        modprobe -r ifb 2>/dev/null || true

        log "Jitter retiré."
        ;;

    *)
        echo "Usage: $0 {start|stop}" >&2
        exit 1
        ;;
esac