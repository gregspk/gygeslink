#!/bin/bash
# GygesLink — USB Gadget Ethernet (CDC-NCM via configfs)
# Compatible Windows 10/11 (driver NCM natif, ZERO installation)
# Compatible Linux/Mac (driver CDC-NCM standard)
# Orange Pi Zero 2W — port USB-C OTG
#
# Prérequis (modules-load.d/gygeslink.conf) :
#   dwc2, libcomposite, usb_f_ncm
#
# Pourquoi CDC-NCM et pas RNDIS ?
#   RNDIS est obsolète sur Windows 10/11 (driver souvent absent).
#   CDC-NCM est standard USB-IF, reconnu nativement par tous les OS modernes.
#   Zéro configuration, zéro driver additionnel.

set -uo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/gygeslink"
UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)

LOG() { echo "[usb-gadget] $*"; }
ERR() { echo "[usb-gadget] ERREUR: $*" >&2; }

# ── Attendre que configfs soit monté ──────────────────────────────
if [ ! -d /sys/kernel/config/usb_gadget ]; then
    ERR "configfs non monté — /sys/kernel/config/usb_gadget/ absent."
    exit 1
fi

# ── Nettoyage si ancien gadget existe ─────────────────────────────
if [ -d "$GADGET_DIR" ]; then
    LOG "Nettoyage de l'ancien gadget..."
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    for link in "$GADGET_DIR"/configs/c.1/*; do
        [ -L "$link" ] && rm "$link" 2>/dev/null || true
    done
    rm "$GADGET_DIR/os_desc/c.1" 2>/dev/null || true
    for dir in functions/ncm.usb0 functions/rndis.usb0 \
               configs/c.1/strings/0x409 configs/c.1 \
               strings/0x409 os_desc; do
        rm -rf "$GADGET_DIR/$dir" 2>/dev/null || true
    done
    rmdir "$GADGET_DIR" 2>/dev/null || true
    LOG "Ancien gadget nettoyé."
fi

# ── Créer le gadget ───────────────────────────────────────────────
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR" || { ERR "Échec cd $GADGET_DIR"; exit 1; }

# ── IDs USB ───────────────────────────────────────────────────────
# VID 0x0525 / PID 0xa4a1 = Linux CDC NCM Gadget
# bDeviceClass 0x02 = Communications (CDC)
echo 0x0525     > idVendor
echo 0xa4a1     > idProduct
echo 0x0100     > bcdDevice
echo 0x0200     > bcdUSB
echo 0x02       > bDeviceClass
echo 0x00       > bDeviceSubClass
echo 0x00       > bDeviceProtocol

mkdir -p strings/0x409
echo "123456789abcdef" > strings/0x409/serialnumber
echo "GygesLink"       > strings/0x409/manufacturer
echo "USB Ethernet"    > strings/0x409/product

# ── Configuration ────────────────────────────────────────────────
mkdir -p configs/c.1
mkdir -p configs/c.1/strings/0x409
echo "NCM"       > configs/c.1/strings/0x409/configuration
echo 250          > configs/c.1/MaxPower

# ── Fonction CDC-NCM ────────────────────────────────────────────
mkdir -p functions/ncm.usb0
echo "ea:11:22:33:44:55" > functions/ncm.usb0/dev_addr
echo "02:11:22:33:44:66" > functions/ncm.usb0/host_addr

# Lier la fonction NCM à la config
ln -sf "$GADGET_DIR/functions/ncm.usb0" "$GADGET_DIR/configs/c.1/"

# ── Activer le gadget sur l'UDC ──────────────────────────────────
sleep 1

if [ -n "$UDC" ]; then
    echo "$UDC" > UDC
    LOG "Gadget CDC-NCM activé sur $UDC"
else
    ERR "Aucun UDC trouvé — câble USB-C branché ?"
    exit 1
fi

sleep 2

# L'interface s'appelle usb0 (créée par le kernel)
if ip link show usb0 > /dev/null 2>&1; then
    LOG "Interface usb0 détectée — gadget prêt."
else
    ERR "Interface usb0 non détectée."
    exit 1
fi