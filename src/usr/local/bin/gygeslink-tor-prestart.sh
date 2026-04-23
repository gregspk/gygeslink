#!/bin/bash
# GygesLink — Configuration de Tor avant démarrage
# Exécuté en ExecStartPre par gygeslink-tor.service
#
# Lit /tmp/gygeslink-tier.status (écrit par gygeslink-wireguard-check.sh)
# et configure torrc en conséquence :
#
#   TIER=2 → ajoute "OutboundBindInterface wg0" dans torrc
#             Tor sortira via le tunnel Mullvad WireGuard
#
#   TIER=1 → supprime "OutboundBindInterface" de torrc
#             Tor sortira directement via wlan0
#
# Note : torrc est sur le rootfs (overlayfs actif en prod).
# Les modifications sont volatiles — réinitialisées à chaque reboot.
# C'est voulu : le tier est redétecté à chaque démarrage.

set -uo pipefail

STATUS_FILE="/run/gygeslink-tier.status"
TORRC="/etc/tor/torrc"

LOG() { echo "[gygeslink-tor-prestart] $*"; }
ERR() { echo "[gygeslink-tor-prestart] ERREUR: $*" >&2; }

# ─────────────────────────────────────────────────────────────────────
# Attendre que le fichier de statut soit disponible
# (gygeslink-wireguard.service doit avoir terminé avant nous)
# ─────────────────────────────────────────────────────────────────────
MAX_WAIT=20
WAITED=0
# Attendre que le fichier existe ET contienne une ligne TIER= valide
while [ ! -f "$STATUS_FILE" ] || ! grep -q "^TIER=" "$STATUS_FILE"; do
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        ERR "Timeout : $STATUS_FILE absent ou invalide après ${MAX_WAIT}s."
        ERR "Sécurité : suppression de OutboundBindInterface (fallback Tier 1)."
        sed -i '/^OutboundBindInterface/d' "$TORRC"
        exit 0
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# ─────────────────────────────────────────────────────────────────────
# Lire le tier détecté
# grep+cut au lieu de source : évite l'exécution de code arbitraire
# si le fichier était compromis (source équivaut à eval).
# ─────────────────────────────────────────────────────────────────────
TIER=$(grep "^TIER=" "$STATUS_FILE" | cut -d= -f2 | tr -dc '12')
TIER="${TIER:-1}"

LOG "Tier détecté : $TIER"

if [ "$TIER" = "2" ]; then
    # ─── Tier 2 : Tor sort via wg0 (Mullvad) ─────────────────────────
    # Supprimer d'abord toute occurrence existante (idempotent)
    sed -i '/^OutboundBindInterface/d' "$TORRC"
    # Ajouter la directive
    echo "OutboundBindInterface wg0" >> "$TORRC"
    LOG "OutboundBindInterface wg0 ajouté à torrc."
    LOG "Tor sortira via le tunnel Mullvad."
else
    # ─── Tier 1 : Tor sort directement via wlan0 ──────────────────────
    sed -i '/^OutboundBindInterface/d' "$TORRC"
    LOG "OutboundBindInterface retiré de torrc."
    LOG "Tor sortira directement via wlan0."
fi

# ─────────────────────────────────────────────────────────────────────
# Bridges obfs4 : activation conditionnelle
# ─────────────────────────────────────────────────────────────────────
# bridges.conf peut être vide (premier boot avant configuration manuelle)
# ou ne contenir que des placeholders.
# Si aucune vraie bridge n'est présente, on désactive UseBridges pour
# éviter que Tor refuse de démarrer — le trafic passera quand même par Tor
# mais sans obfuscation obfs4 (moins discret, toujours anonyme).
#
# Format d'une vraie bridge : "Bridge obfs4 A.B.C.D:PORT FINGERPRINT cert=..."
# Un placeholder contient "REMPLACER" — on le détecte pour l'ignorer.

BRIDGES_CONF="/data/gygeslink/bridges.conf"
HAS_VALID_BRIDGES=0

    if [ -f "$BRIDGES_CONF" ]; then
        # Chercher des lignes Bridge qui ne sont pas des placeholders
        if grep -qE "^Bridge obfs4 [0-9]" "$BRIDGES_CONF" 2>/dev/null; then
            HAS_VALID_BRIDGES=1
        fi
        # Si un placeholder subsiste, considérer le fichier invalide
        # (évite que Tor ne plante au boot avec une ligne partiellement remplie)
        if grep -qE "REMPLACER" "$BRIDGES_CONF" 2>/dev/null; then
            HAS_VALID_BRIDGES=0
        fi
    fi

if [ "$HAS_VALID_BRIDGES" = "1" ]; then
    # Bridges valides présents → activer UseBridges
    sed -i 's/^UseBridges 0/UseBridges 1/' "$TORRC" 2>/dev/null || true
    LOG "Bridges obfs4 détectés — UseBridges actif."
else
    # Pas de bridges → désactiver UseBridges pour éviter l'échec de Tor
    sed -i 's/^UseBridges 1/UseBridges 0/' "$TORRC" 2>/dev/null || true
    ERR "ATTENTION : Aucun bridge obfs4 valide dans $BRIDGES_CONF."
    ERR "Tor démarrera SANS obfuscation obfs4 (visible comme Tor par le FAI)."
    ERR "Ajoutez 3 bridges obfs4 dans $BRIDGES_CONF et redémarrez."
fi

exit 0
