#!/bin/bash
# GygesLink — Détection du tier et montage WireGuard
# Exécuté par gygeslink-wireguard.service (Type=oneshot)
#
# Résultat écrit dans /tmp/gygeslink-tier.status :
#   TIER=1  →  Tor sortira directement via wlan0 (Classic)
#   TIER=2  →  Tor sortira via wg0 Mullvad (Advanced)
#
# Le script gygeslink-tor-prestart.sh lit ce fichier avant le démarrage
# de Tor pour configurer OutboundBindInterface en conséquence.

set -uo pipefail

WG_CONF="/data/gygeslink/wg0.conf"
STATUS_FILE="/tmp/gygeslink-tier.status"

LOG()  { echo "[gygeslink-wireguard] $*"; }
ERR()  { echo "[gygeslink-wireguard] ERREUR: $*" >&2; }

write_status() {
    echo "TIER=$1" > "$STATUS_FILE"
    LOG "Tier détecté : $1"
}

# ─────────────────────────────────────────────────────────────────────
# CAS 1 : Pas de config WireGuard → Tier 1 (Classic)
# ─────────────────────────────────────────────────────────────────────
if [ ! -f "$WG_CONF" ]; then
    LOG "Aucune config WireGuard trouvée dans $WG_CONF."
    LOG "Mode Classic (Tier 1) : Tor sortira directement via wlan0."
    write_status 1
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────
# CAS 2 : Config présente — tenter Tier 2 (Advanced)
# ─────────────────────────────────────────────────────────────────────
LOG "Config WireGuard trouvée — tentative Tier 2 (Mullvad)..."

# Extraire l'endpoint Mullvad depuis la config WireGuard
# Supporte les deux formats : "Endpoint = IP:PORT" et "Endpoint=IP:PORT"
# sed normalise le séparateur = avec ou sans espaces, puis on retire le port
ENDPOINT=$(grep "^Endpoint" "$WG_CONF" 2>/dev/null \
    | sed 's/^Endpoint[[:space:]]*=[[:space:]]*//' \
    | cut -d: -f1 \
    | tr -d '[:space:]')

if [ -z "$ENDPOINT" ]; then
    ERR "Impossible d'extraire l'endpoint depuis $WG_CONF."
    write_status 1
    exit 0
fi

LOG "Endpoint Mullvad : $ENDPOINT"

# ─── Test de joignabilité via DNS ─────────────────────────────────────
# On utilise getent hosts (résolution DNS) plutôt que ping (ICMP).
# Raison : Mullvad et de nombreux VPN bloquent ICMP sur leurs endpoints.
# Un ping raté ne signifie pas que le serveur est injoignable.
# La résolution DNS est beaucoup plus fiable.
LOG "Test de joignabilité de l'endpoint..."

if ! timeout 5 getent hosts "$ENDPOINT" > /dev/null 2>&1; then
    ERR "Endpoint '$ENDPOINT' non résolvable. FAI bloque-t-il le DNS ?"
    LOG "Fallback Tier 1 : Tor sortira via wlan0."
    write_status 1
    exit 0
fi

LOG "Endpoint joignable."

# ─── Tentative de montage WireGuard ───────────────────────────────────
LOG "Montage de l'interface wg0..."

# S'assurer que wg0 n'est pas déjà monté (reboot partiel, etc.)
wg-quick down wg0 2>/dev/null || true

if ! timeout 15 wg-quick up wg0 > /tmp/wg-up.log 2>&1; then
    ERR "wg-quick up wg0 a échoué. Voir /tmp/wg-up.log."
    LOG "Fallback Tier 1 : Tor sortira via wlan0."
    write_status 1
    exit 0
fi

# ─── Vérification que wg0 a bien une IP ───────────────────────────────
# wg-quick peut "réussir" sans que l'interface ait une IP valide
# (ex : config corrompue, clé expirée).
if ! ip addr show wg0 2>/dev/null | grep -q "inet "; then
    ERR "Interface wg0 montée mais sans adresse IP. Config invalide ?"
    wg-quick down wg0 2>/dev/null || true
    LOG "Fallback Tier 1 : Tor sortira via wlan0."
    write_status 1
    exit 0
fi

WG_IP=$(ip addr show wg0 | awk '/inet / {print $2}')
LOG "Interface wg0 active : $WG_IP"

# ─── Succès Tier 2 ────────────────────────────────────────────────────
write_status 2
LOG "Tier 2 actif — Tor sortira via wg0 (Mullvad)."
exit 0
