#!/bin/bash
# GygesLink — Nettoyage du gadget USB configfs à l'arrêt du service.
# Configfs est un pseudo-filesystem — les suppressions doivent se faire
# dans l'ordre inverse de la création. Ce script gère ces contraintes.
#
# Référence : https://github.com/adam-burns/devuan-pi-gadgeteer

GADGET_DIR="/sys/kernel/config/usb_gadget/gygeslink"

[ -d "$GADGET_DIR" ] || exit 0

# 1. Détacher l'UDC (obligatoire avant toute suppression)
echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
sleep 0.5

# 2. Retirer les liens symboliques config -> function
for lnk in "$GADGET_DIR"/configs/c.1/*; do
    [ -L "$lnk" ] && rm -f "$lnk"
done 2>/dev/null || true

# 3. Retirer le lien os_desc -> config
rm -f "$GADGET_DIR/os_desc/c.1" 2>/dev/null || true

# 4. Supprimer les configs (et leurs strings)
rm -rf "$GADGET_DIR/configs" 2>/dev/null || true

# 5. Supprimer les fonctions (y compris os_desc/interface.rndis)
rm -rf "$GADGET_DIR/functions" 2>/dev/null || true

# 6. Supprimer les strings du gadget
rm -rf "$GADGET_DIR/strings" 2>/dev/null || true

# 7. Supprimer os_desc
rm -rf "$GADGET_DIR/os_desc" 2>/dev/null || true

# 8. Enfin, supprimer le gadget lui-même
rmdir "$GADGET_DIR" 2>/dev/null || true

exit 0
