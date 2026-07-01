#!/bin/bash
# GygesLink - Configuration de Tor avant démarrage
# Exécuté en ExecStartPre par gygeslink-tor.service
#
# Active/désactive UseBridges selon la présence de bridges obfs4 valides
# dans /data/gygeslink/bridges.conf.
#
# Note : torrc est sur le rootfs (overlayfs actif en prod).
# Les modifications sont volatiles; réinitialisées à chaque reboot.

set -euo pipefail

TORRC="/etc/tor/torrc"

LOG() { echo "[gygeslink-tor-prestart] $*"; }
ERR() { echo "[gygeslink-tor-prestart] ERREUR: $*" >&2; }

# ─────────────────────────────────────────────────────────────────────
# Bridges obfs4 : activation conditionnelle
# ─────────────────────────────────────────────────────────────────────
# bridges.conf peut être vide (premier boot avant configuration manuelle)
# ou ne contenir que des placeholders.
# Si aucune vraie bridge n'est présente, on désactive UseBridges pour
# éviter que Tor refuse de démarrer; le trafic passera quand même par Tor
# mais sans obfuscation obfs4 (moins discret, toujours anonyme).
#
# Format d'une vraie bridge : "Bridge obfs4 A.B.C.D:PORT FINGERPRINT cert=..."
# Un placeholder contient "REMPLACER", on le détecte pour l'ignorer.

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
    sed -i 's/^UseBridges .*/UseBridges 1/' "$TORRC"
    LOG "Bridges obfs4 détectés — UseBridges actif."
else
    sed -i 's/^UseBridges .*/UseBridges 0/' "$TORRC"
    ERR "ATTENTION : Aucun bridge obfs4 valide dans $BRIDGES_CONF."
    ERR "Tor démarrera SANS obfuscation obfs4 (visible comme Tor par le FAI)."
    ERR "Ajoutez 3 bridges obfs4 dans $BRIDGES_CONF et redémarrez."
fi

exit 0
