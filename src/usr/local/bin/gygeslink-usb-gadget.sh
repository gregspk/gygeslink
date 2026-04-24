#!/bin/bash
# GygesLink — USB Gadget Ethernet (RNDIS via configfs)
# Compatible Windows 10/11 (driver RNDIS natif + OS descriptors Microsoft)
# Orange Pi Zero 2W — port USB-C OTG
#
# Prérequis (modules-load.d/gygeslink.conf) :
#   dwc2, libcomposite, usb_f_rndis
#
# Ce script crée un gadget RNDIS via configfs. L'interface usb0
# apparaît côté Pi et le PC voit une carte réseau USB "Remote NDIS".
#
# NE PAS utiliser g_ether (obsolète sur Armbian 26.x).
# Ce script est appelé par gygeslink-usb-gadget.service.

set -uo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/gygeslink"
UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)

LOG() { echo "[usb-gadget] $*"; }
ERR() { echo "[usb-gadget] ERREUR: $*" >&2; }

# ── Attendre que configfs soit monté ──────────────────────────────
if [ ! -d /sys/kernel/config/usb_gadget ]; then
    ERR "configfs non monté — /sys/kernel/config/usb_gadget/ absent."
    ERR "Vérifier que libcomposite est chargé et configfs monté."
    exit 1
fi

# ── Nettoyage si ancien gadget existe ─────────────────────────────
# configfs ne supporte PAS rm -rf : il faut démonter dans l'ordre inverse.
if [ -d "$GADGET_DIR" ]; then
    LOG "Nettoyage de l'ancien gadget..."
    # Détacher l'UDC en premier (coupe la connexion USB)
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    # Retirer les liens symboliques (fonctions → configs)
    for link in "$GADGET_DIR"/configs/c.1/*; do
        [ -L "$link" ] && rm "$link" 2>/dev/null || true
    done
    # Retirer le lien os_desc → config
    rm "$GADGET_DIR/os_desc/c.1" 2>/dev/null || true
    # Supprimer les sous-répertoires (fonctions, configs, strings, os_desc)
    for dir in functions rndis.usb0 configs/c.1/strings/0x409 \
               configs/c.1 strings/0x409 os_desc; do
        rm -rf "$GADGET_DIR/$dir" 2>/dev/null || true
    done
    # Supprimer le gadget lui-même
    rmdir "$GADGET_DIR" 2>/dev/null || true
    LOG "Ancien gadget nettoyé."
fi

# ── Créer le gadget ───────────────────────────────────────────────
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR" || { ERR "Échec cd $GADGET_DIR"; exit 1; }

# ── IDs USB : RNDIS Gadget Windows-compatible ────────────────────
# vendor  0x0525 = Linux Foundation
# product 0xa4a2 = Linux USB Ethernet/RNDIS Gadget
# bDeviceClass 0xEF = Miscellaneous (composite device)
# bDeviceSubClass 0x02 = Common Class
# bDeviceProtocol 0x01 = Interface Association Descriptor
# Ces valeurs sont CRITIQUES pour que Windows reconnaisse le RNDIS
# sans driver additionnel.
echo 0x0525     > idVendor
echo 0xa4a2     > idProduct
echo 0x0100     > bcdDevice
echo 0x0200     > bcdUSB
echo 0xEF       > bDeviceClass
echo 0x02       > bDeviceSubClass
echo 0x01       > bDeviceProtocol

mkdir -p strings/0x409
echo "123456789abcdef" > strings/0x409/serialnumber
echo "GygesLink"       > strings/0x409/manufacturer
echo "USB Ethernet"     > strings/0x409/product

# ── Configuration ────────────────────────────────────────────────
mkdir -p configs/c.1
mkdir -p configs/c.1/strings/0x409
echo "RNDIS"      > configs/c.1/strings/0x409/configuration
echo 250          > configs/c.1/MaxPower

# ── Fonction RNDIS ───────────────────────────────────────────────
mkdir -p functions/rndis.usb0
echo "ea:11:22:33:44:55" > functions/rndis.usb0/dev_addr
echo "02:11:22:33:44:66" > functions/rndis.usb0/host_addr

# ── OS Descriptors Microsoft (RNDIS automatique sur Windows 10/11)
# Sans ces descripteurs, Windows peut ne pas charger le driver RNDIS
# et afficher "Périphérique USB inconnu" dans le gestionnaire.
mkdir -p os_desc
echo 1       > os_desc/use
echo 0xcd    > os_desc/b_vendor_code
echo "MSFT100" > os_desc/qw_sign

# Descripteur d'interface RNDIS pour Microsoft
mkdir -p functions/rndis.usb0/os_desc/interface.rndis
echo "RNDIS" > functions/rndis.usb0/os_desc/interface.rndis/compat_id

# Lier la config aux OS descriptors
ln -sf "$GADGET_DIR/configs/c.1" "$GADGET_DIR/os_desc/"

# Lier la fonction RNDIS à la config
ln -sf "$GADGET_DIR/functions/rndis.usb0" "$GADGET_DIR/configs/c.1/"

# ── Activer le gadget sur l'UDC ──────────────────────────────────
sleep 1

if [ -n "$UDC" ]; then
    echo "$UDC" > UDC
    LOG "Gadget RNDIS activé sur $UDC"
else
    ERR "Aucun UDC trouvé — le câble USB-C est-il branché ?"
    ERR "Contrôleur USB OTG (dwc2) non détecté."
    exit 1
fi

# Attendre que l'interface usb0 apparaisse
sleep 2

if ip link show usb0 > /dev/null 2>&1; then
    LOG "Interface usb0 détectée — gadget prêt."
else
    ERR "Interface usb0 non détectée après activation du gadget."
    ERR "Vérifier les modules dwc2 + usb_f_rndis."
    exit 1
fi