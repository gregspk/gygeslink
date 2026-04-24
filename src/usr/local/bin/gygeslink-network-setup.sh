#!/bin/bash
# GygesLink — Script de configuration réseau au boot
# Exécuté par gygeslink-network-setup.service (Type=oneshot)
#
# ORDRE CRITIQUE DE SÉCURITÉ :
#   1. Désactiver NetworkManager pour éviter les conflits
#   2. Configurer usb0 (côté PC — USB gadget via configfs/RNDIS)
#   3. Appliquer iptables DROP immédiatement (fail-close atomique)
#   4. Randomiser MAC wlan0 (avant toute association WiFi)
#   5. Se connecter au réseau WiFi via wpa_supplicant + dhclient

set -uo pipefail

LOG() { echo "[gygeslink-network] $*"; }
ERR() { echo "[gygeslink-network] ERREUR: $*" >&2; }

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 0 : Désactiver NetworkManager pour éviter les conflits
# NetworkManager gère wlan0 et usb0 par défaut sur Armbian.
# Il entre en conflit avec wpa_supplicant et les ip link/ip addr manuels.
# ─────────────────────────────────────────────────────────────────────
LOG "Désactivation de NetworkManager sur wlan0 et usb0..."

# NetworkManager est déjà exclu via conf drop-in, mais on s'assure
# qu'il n'interfère pas pendant ce script.
nmcli device set wlan0 managed no 2>/dev/null || true
nmcli device set usb0 managed no 2>/dev/null || true
# Tuer wpa_supplicant géré par NetworkManager s'il est en cours
killall wpa_supplicant 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 0b : Chargement configuration personnalisée
# (avant usb0 config pour USB0_ADDR potentiel)
# ─────────────────────────────────────────────────────────────────────
USB0_ADDR="192.168.100.1/24"
WIFI_TIMEOUT=20
DHCP_TIMEOUT=30

if [ -f /data/gygeslink/network.conf ]; then
    LOG "Chargement de /data/gygeslink/network.conf..."
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${key// /}" ]] && continue
        key="${key// /}"
        val="${val// /}"
        case "$key" in
            USB0_ADDR)    USB0_ADDR="$val"    ;;
            WIFI_TIMEOUT) WIFI_TIMEOUT="$val" ;;
            DHCP_TIMEOUT) DHCP_TIMEOUT="$val" ;;
        esac
    done < /data/gygeslink/network.conf
fi

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 1 : Configurer usb0 (interface côté PC)
# usb0 est créée par gygeslink-usb-gadget.sh via configfs (RNDIS).
# Le gadget service doit avoir démarré AVANT ce script.
# ─────────────────────────────────────────────────────────────────────
LOG "Configuration usb0 (côté PC, USB gadget)..."

# Vérifier que usb0 existe
if ! ip link show usb0 > /dev/null 2>&1; then
    ERR "Interface usb0 non trouvée — le gadget USB n'est pas prêt."
    ERR "Vérifier gygeslink-usb-gadget.service et les modules dwc2/usb_f_rndis."
    exit 1
fi

ip link set usb0 up
ip addr flush dev usb0 2>/dev/null || true
ip addr add "$USB0_ADDR" dev usb0

# Activer le routage IP (le boîtier doit faire transiter les paquets)
sysctl -w net.ipv4.ip_forward=1 > /dev/null

# Désactiver IPv6 sur toutes les interfaces — prévient tout leak.
# Les règles ip6tables-drop.rules constituent une deuxième barrière,
# mais désactiver IPv6 au niveau kernel est plus sûr.
sysctl -w net.ipv6.conf.all.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.default.disable_ipv6=1 > /dev/null
sysctl -w net.ipv6.conf.lo.disable_ipv6=1 > /dev/null

LOG "usb0 configuré : $USB0_ADDR"

# Relancer dnsmasq pour qu'il prenne en compte usb0 (DHCP côté PC).
# Le fichier /etc/dnsmasq.d/gygeslink-usb0.conf configure le DHCP
# sur 192.168.100.100-110/24 pour le PC branché en USB-C.
# On utilise restart (pas start) car dnsmasq peut déjà tourner.
systemctl restart dnsmasq 2>/dev/null || true
LOG "dnsmasq (re)démarré — DHCP actif sur usb0."

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 2 : Appliquer les règles iptables DROP (fail-close)
# CRITIQUE : appliqué ici de façon atomique, immédiatement après
# la configuration de usb0, avant toute autre opération réseau.
# Garantit qu'aucun paquet ne peut traverser le boîtier en clair.
# ─────────────────────────────────────────────────────────────────────
LOG "Application des règles iptables fail-close..."

if ! iptables-restore < /etc/gygeslink/iptables-drop.rules; then
    ERR "Échec de iptables-restore — arrêt."
    exit 1
fi

if ! ip6tables-restore < /etc/gygeslink/ip6tables-drop.rules; then
    ERR "Échec de ip6tables-restore — arrêt."
    exit 1
fi

LOG "Règles fail-close actives — tout trafic bloqué sauf DHCP, SSH et loopback."

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 2b : Mode setup — ouverture temporaire du portail
# Si le boîtier n'a pas encore été configuré (setup-done absent),
# le portail Flask doit être accessible depuis le PC via usb0 (HTTPS).
# Les règles DROP du step 2 bloquent tout ; on ajoute temporairement
# une exception pour le portail (tcp/443 sur usb0 uniquement).
# Ces règles sont retirées automatiquement par gygeslink-setup.service
# à l'arrêt du portail (ou au prochain reboot).
# ─────────────────────────────────────────────────────────────────────
if [ ! -f /data/gygeslink/setup-done ]; then
    LOG "Mode setup détecté : ouverture temporaire du portail HTTPS sur usb0."
    iptables -I INPUT -i usb0 -p tcp --dport 443 -j ACCEPT
    iptables -I OUTPUT -o usb0 -p tcp --sport 443 -j ACCEPT
    LOG "Portail temporairement accessible sur https://192.168.100.1:443"
fi

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 3 : Randomiser l'adresse MAC de wlan0
# wlan0 est l'interface côté routeur FAI (WiFi client).
# L'adresse MAC est visible par le routeur et potentiellement loguée.
# En la randomisant à chaque boot, on évite le tracking matériel.
# DOIT être fait AVANT que wlan0 s'associe au point d'accès.
# ─────────────────────────────────────────────────────────────────────
LOG "Randomisation MAC wlan0..."

ip link set wlan0 down 2>/dev/null || true
if macchanger -r wlan0 > /dev/null 2>&1; then
    NEW_MAC=$(ip link show wlan0 | awk '/ether/ {print $2}')
    LOG "Nouveau MAC wlan0 : $NEW_MAC"
else
    ERR "macchanger indisponible — MAC non randomisé."
fi

# ─────────────────────────────────────────────────────────────────────
# ÉTAPE 4 : Connexion WiFi via wpa_supplicant + dhclient
# wifi.conf contient les credentials WPA2 (SSID + mot de passe).
# Créé par gygeslink-setup-portal.py lors du premier setup.
# Si absent : mode setup requis (pas de connexion internet possible).
# ─────────────────────────────────────────────────────────────────────
WIFI_CONF="/data/gygeslink/wifi.conf"

if [ ! -f "$WIFI_CONF" ]; then
    ERR "Aucune config WiFi ($WIFI_CONF) — setup requis."
    ERR "Le boîtier reste en fail-close jusqu'au setup."
    # Ne pas exit 1 ici : le portail setup a besoin de usb0,
    # et le mode setup a déjà ouvert le 443. Mais Tor ne doit pas démarrer.
    # On sort proprement sans lancer wpa_supplicant.
    exit 0
fi

LOG "Connexion WiFi (wpa_supplicant)..."

# Lancer wpa_supplicant en arrière-plan
# -B = background, -i = interface, -c = config, -P = PID file
if ! wpa_supplicant -B -i wlan0 -c "$WIFI_CONF" -P /run/wpa_supplicant-wlan0.pid 2>/dev/null; then
    ERR "wpa_supplicant a échoué — vérifier $WIFI_CONF."
    exit 1
fi

# Attendre l'association WiFi (max WIFI_TIMEOUT secondes, défaut 20s)
WAITED=0
while [ "$WAITED" -lt "$WIFI_TIMEOUT" ]; do
    if wpa_cli -i wlan0 status 2>/dev/null | grep -q "wpa_state=COMPLETED"; then
        LOG "WiFi associé."
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

if [ "$WAITED" -ge "$WIFI_TIMEOUT" ]; then
    ERR "Association WiFi timeout (${WIFI_TIMEOUT}s) — SSID joignable ?"
    exit 1
fi

# Obtenir une IP via dhclient sur wlan0
# -1 : tentative unique, pas de retry infini
# --no-pid : ne pas écraser un PID existant
LOG "Obtention IP sur wlan0 via dhclient..."

if ! timeout "$DHCP_TIMEOUT" dhclient -1 wlan0 2>/dev/null; then
    ERR "DHCP wlan0 timeout — routeur joignable ?"
    exit 1
fi

WLAN0_IP=$(ip addr show wlan0 | awk '/inet / {print $2}')
LOG "wlan0 configuré : $WLAN0_IP"

# Empêcher dhclient d'avoir écrasé /etc/resolv.conf avec les DNS du FAI.
# Le boîtier ne résout JAMAIS via le FAI : Tor fait sa propre résolution.
# Si resolv.conf contient les DNS du FAI, c'est un metadata leak visible.
echo "nameserver 127.0.0.1" > /etc/resolv.conf
LOG "DNS local restauré (127.0.0.1) — pas de fuite vers le FAI."

LOG "Configuration réseau terminée."
LOG "État : fail-close actif, Tor pas encore démarré."