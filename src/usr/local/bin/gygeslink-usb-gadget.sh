# GygesLink — USB Gadget Ethernet (g_ether moderne via configfs)
# Compatible Windows 10/11 (driver RNDIS natif)
# Orange Pi Zero 2W — port USB-C OTG

GADGET_DIR="/sys/kernel/config/usb_gadget/gygeslink"
UDC=$(ls /sys/class/udc/ | head -1)

sleep 2

# Nettoyage si ancien gadget existe (erreurs attendues, on ignore)
if [ -d "$GADGET_DIR" ]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    # Les rm peuvent échouer sur fichiers en lecture seule dans configfs
    rm -rf "$GADGET_DIR" 2>/dev/null || true
fi

mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR" || { echo "[usb-gadget] Échec cd $GADGET_DIR"; exit 1; }

# ── IDs USB : RNDIS Gadget Windows-compatible ───────
# vendor  0x0525 = Linux Foundation
# product 0xa4a2 = Linux USB Ethernet/RNDIS Gadget
# Le driver Windows RNDIS reconnaît cet IDV nativement.
echo 0x0525 > idVendor
echo 0xa4a2 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "123456789abcdef" > strings/0x409/serialnumber
echo "GygesLink" > strings/0x409/manufacturer
echo "USB Ethernet" > strings/0x409/product

mkdir -p configs/c.1
mkdir -p configs/c.1/strings/0x409
echo "RNDIS" > configs/c.1/strings/0x409/configuration

# Ajouter MaxPower (requis par certains OS)
echo 250 > configs/c.1/MaxPower

# ── Fonction RNDIS (Windows) ────────────────────────
mkdir -p functions/rndis.usb0
echo "ea:11:22:33:44:55" > functions/rndis.usb0/dev_addr   # MAC côté Pi
echo "02:11:22:33:44:66" > functions/rndis.usb0/host_addr  # MAC côté PC

# Activer le mode RNDIS (requis pour Windows 10/11)
mkdir -p os_desc
echo 1 > os_desc/use
echo 0xcd > os_desc/b_vendor_code
echo "MSFT100" > os_desc/qw_sign

mkdir -p functions/rndis.usb0/os_desc/interface.rndis
ln -sf configs/c.1 "$GADGET_DIR/os_desc"

ln -sf "$GADGET_DIR/functions/rndis.usb0" "$GADGET_DIR/configs/c.1/"

# Activer le gadget sur l'UDC
if [ -n "$UDC" ]; then
    echo "$UDC" > UDC
    echo "[usb-gadget] Gadget RNDIS activé sur $UDC"
else
    echo "[usb-gadget] ERREUR : aucun UDC trouvé."
    exit 1
fi

sleep 2
