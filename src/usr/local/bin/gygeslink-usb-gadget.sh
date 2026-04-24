#!/bin/bash
# GygesLink — USB Gadget RNDIS via configfs
# Orange Pi Zero 2W (Allwinner H618) — port USB-C OTG
#
# Ce script crée un gadget USB composite configuré comme périphérique
# RNDIS (Remote NDIS), visible par le PC comme une carte réseau USB.
#
# PRÉREQUIS (chargés par modules-load.d) :
#   dwc2          : contrôleur USB OTG Allwinner
#   libcomposite  : framework configfs
#   usb_f_rndis   : fonction RNDIS
#
# Ce script est appelé par gygeslink-usb-gadget.service AVANT
# gygeslink-network-setup.service.

set -uo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/gygeslink"
UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)

LOG() { echo "[usb-gadget] $*"; }
ERR() { echo "[usb-gadget] ERREUR: $*" >&2; }

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 1 : Nettoyage propre si ancien gadget existe
# Référence : https://github.com/adam-burns/devuan-pi-gadgeteer
# Ordre inverse strict pour éviter EBUSY (configfs n'autorise pas rm -rf)
# ─────────────────────────────────────────────────────────────────────
if [ -d "$GADGET_DIR" ]; then
    LOG "Ancien gadget trouvé, nettoyage..."
    # 1. Désactiver le gadget
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    sleep 0.5
    # 2. Retirer les liens symlinks config -> function
    for lnk in "$GADGET_DIR"/configs/c.1/*; do
        [ -L "$lnk" ] && rm -f "$lnk"
    done 2>/dev/null || true
    # 3. Retirer le lien os_desc -> config
    rm -f "$GADGET_DIR/os_desc/c.1" 2>/dev/null || true
    # 4. Retirer les répertoires configs et functions
    rm -rf "$GADGET_DIR/configs" 2>/dev/null || true
    rm -rf "$GADGET_DIR/functions" 2>/dev/null || true
    # 5. Retirer strings et os_desc
    rm -rf "$GADGET_DIR/strings" 2>/dev/null || true
    rm -rf "$GADGET_DIR/os_desc" 2>/dev/null || true
    # 6. Enfin retirer le gadget
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 2 : Création du gadget
# ─────────────────────────────────────────────────────────────────────
LOG "Création du gadget RNDIS..."

mkdir -p "$GADGET_DIR" || { ERR "Impossible de créer $GADGET_DIR"; exit 1; }
cd "$GADGET_DIR" || { ERR "Impossible de cd $GADGET_DIR"; exit 1; }

# Identifiants USB — VID/PID Linux RNDIS (reconnus par Windows)
echo 0x0525 > idVendor
echo 0xa4a2 > idProduct

# Version du device et USB
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

# Composite device class — requis pour que Windows reconnaisse un
# périphérique multifonction (RNDIS + autres potentiels)
echo 0xEF > bDeviceClass
echo 0x02 > bDeviceSubClass
echo 0x01 > bDeviceProtocol

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 3 : Strings USB (descripteurs texte)
# ─────────────────────────────────────────────────────────────────────
mkdir -p strings/0x409
echo "123456789abcdef" > strings/0x409/serialnumber
echo "GygesLink" > strings/0x409/manufacturer
echo "RNDIS Ethernet" > strings/0x409/product

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 4 : Configuration
# ─────────────────────────────────────────────────────────────────────
mkdir -p configs/c.1
mkdir -p configs/c.1/strings/0x409
echo "RNDIS Config" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 5 : Fonction RNDIS
# ─────────────────────────────────────────────────────────────────────
mkdir -p functions/rndis.usb0

# MAC addresses (côté Pi = dev_addr, côté PC = host_addr)
echo "ea:11:22:33:44:55" > functions/rndis.usb0/dev_addr
echo "02:11:22:33:44:66" > functions/rndis.usb0/host_addr

# Lier la fonction à la configuration
ln -sf "$GADGET_DIR/functions/rndis.usb0" "$GADGET_DIR/configs/c.1/"

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 6 : OS Descriptors Microsoft (compatibilité Windows)
# Référence : P4wnP1 / devuan-pi-gadgeteer
# Windows refuse de charger RNDIS sans ces descriptors.
# ─────────────────────────────────────────────────────────────────────
mkdir -p os_desc
echo 1       > os_desc/use
echo 0xcd    > os_desc/b_vendor_code
echo "MSFT100" > os_desc/qw_sign

# OS descriptors au niveau interface RNDIS (obligatoire pour Windows)
mkdir -p functions/rndis.usb0/os_desc/interface.rndis
echo "RNDIS"   > functions/rndis.usb0/os_desc/interface.rndis/compatible_id
echo "5162001" > functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id

# Associer la configuration à os_desc : os_desc/c.1 -> configs/c.1
# Cela indique à Windows que la config c.1 est la "default" pour les OS descriptors.
ln -sf "$GADGET_DIR/configs/c.1" "$GADGET_DIR/os_desc/c.1"

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 7 : Activation (bind UDC)
# ─────────────────────────────────────────────────────────────────────
if [ -z "$UDC" ]; then
    ERR "Aucun UDC disponible. dwc2 activé ? Câble branché ?"
    exit 1
fi

echo "$UDC" > UDC || { ERR "Échec de l'activation sur $UDC"; exit 1; }

LOG "Gadget RNDIS actif sur UDC=$UDC"
sleep 2

LOG "usb0 devrait apparaître dans les prochaines secondes."

exit 0
