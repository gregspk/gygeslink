# GygesLink — Guide de Déploiement sur Orange Pi Zero 2W

## Contexte
Ce guide suppose :
- Tu flashes une **SD vierge** avec **Armbian 26.2.0 Trixie CLI (minimal)**.
- Tu configures ton **WiFi** lors du premier boot (setup initial Armbian, ou via `wpa_supplicant.conf` pré-créé sur la partition BOOT).
- Le reste du déploiement se fait en **SSH**.
- Une fois terminé, tu branches le **port USB-C OTG** au PC Windows (sans débrancher l'alimentation) pour tester.

> **Note hardware** : l'Orange Pi Zero 2W a **2 ports USB-C**.
> - **Port alimentation (5V)** — reste branché secteur en permanence.
> - **Port OTG (USB2.0)** — connecté au PC Windows pour le test du gadget RNDIS.
> Les deux peuvent être branchés simultanément.

---

## Étape 0 — Prérequis

| Item | Détail |
|------|--------|
| Carte SD | ≥ 16 Go, classe 10 |
| Image Armbian | `Armbian_community_26.2.0-trunk.792_Orangepizero2w_trixie_current_6.18.24_minimal.img` |
| Flash tool | Balena Etcher, Rufus, ou `dd` |
| Alimentation | Câble USB-C + bloc 5V/2A (branché sur le port **alimentation**) |
| Câble USB-C données | pour brancher le boîtier au PC Windows (port **OTG**) |
| WiFi credentials | SSID + mot de passe |

> **Note hardware** : l'Orange Pi Zero 2W a **2 ports USB-C**.
> - Un port alimentation (5V) — reste branché secteur en permanence.
> - Un port OTG (USB gadget) — connecté au PC Windows pour le test final.
> Les deux peuvent être branchés simultanément.

---

## Étape 1 — Flash de la SD

1. Télécharge l'image Armbian CLI Bookworm pour Orange Pi Zero 2W.
2. Flash avec Balena Etcher (ou Rufus) sur la SD.
3. Insère la SD dans le boîtier.
4. **Ne branche PAS encore le câble USB-C au PC.** Branche l'alimentation secteur uniquement.
5. Attends ~2 min le premier boot.

---

## Étape 2 — Premier boot et configuration initiale

Armbian va te demander interactivement (sur le port série HDMI ou si tu as un écran) :

1. **Créer un utilisateur** (ex. `pi`) et définir un mot de passe.
2. **Choisir le timezone** (ex. `Europe/Paris`).
3. **Configurer le WiFi**.
   - Soit via l'interface interactive Armbian (`armbian-config` → Network → WiFi).
   - Soit en SSH (étape 3) en éditant `/etc/wpa_supplicant/wpa_supplicant.conf`.

> **Important** : le WiFi doit être fonctionnel à la fin de cette étape pour que SSH via le réseau local soit possible.

---

## Étape 3 — Connexion SSH

Trouve l'IP du boîtier sur ton réseau (interface du routeur, ou `nmap -sn 192.168.1.0/24`, ou Angry IP Scanner).

```bash
# Remplace x.x.x.x par l'IP de ton Orange Pi
ssh pi@192.168.1.xxx
```

---

## Étape 4 — Préparation du système (en SSH)

Toutes les commandes ci-dessous sont à exécuter en root ou avec `sudo`.

### 4.1 Passer en root
```bash
sudo -i
```

### 4.2 Mettre à jour les packages
```bash
apt update && apt upgrade -y
```

### 4.3 Installer les dépendances manquantes
```bash
apt install -y \
  iptables dhcpcd dnsmasq macchanger wpasupplicant \
  tor obfs4proxy wireguard-tools python3-pip python3-libgpiod \
  i2c-tools git
```

> **Note pour Debian 12** : `libgpiod2` s'appelle peut-être `python3-libgpiod`. Si le paquet `libgpiod2` n'existe pas, essaie `python3-libgpiod` à la place.

### 4.4 Désactiver NetworkManager (si présent)
Sur Armbian minimal, NetworkManager est souvent absent (WiFi géré directement par `systemd-networkd` ou `wpa_supplicant`). Si la commande ci-dessous retourne `Unit not loaded`, c'est normal, passe à l'étape suivante.

```bash
systemctl stop NetworkManager 2>/dev/null || true
systemctl disable NetworkManager 2>/dev/null || true
systemctl mask NetworkManager 2>/dev/null || true
```

### 4.5 Vérifier que le WiFi est fonctionnel
```bash
ip link show wlan0
iw wlan0 link
```

Si `wlan0` est down, relève manuellement :
```bash
ip link set wlan0 up
wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/wpa_supplicant.conf
dhclient wlan0
```

Assure-toi que tu as toujours une connexion internet avant de continuer.

---

## Étape 5 — Déploiement du code GygesLink

### 5.1 Cloner le repo
```bash
cd /opt
git clone https://github.com/gregspk/gygeslink.git
cd gygeslink
```

### 5.2 Copier les fichiers vers le système
```bash
# Copie récursive des fichiers source vers /
cp -rv src/* /
```

### 5.3 Rendre les scripts exécutables
```bash
chmod +x /usr/local/bin/gygeslink-*.sh
chmod +x /usr/local/bin/gygeslink-*.py
chmod +x /usr/local/bin/noise_generator.py
chmod +x /usr/local/bin/gygeslink-tor-prestart.sh
chmod +x /usr/local/bin/gygeslink-wireguard-check.sh
```

---

## Étape 6 — Création de la partition persistante `/data`

Le dossier `/data` stocke les configs persistantes (WiFi, setup-done, bridges, WireGuard). Sur Armbian, une partition peut déjà être montée sur `/media/mmcboot` ou similaire. Si tu veux une partition dédiée :

### Option A — Utiliser un dossier sur la racine (plus simple pour le test)
```bash
mkdir -p /data/gygeslink
chmod 700 /data/gygeslink
```

### Option B — Créer une partition persistante (recommandé en prod)
Si tu as de l'espace non alloué sur la SD :
```bash
# Utilise fdisk pour créer une nouvelle partition, puis :
mkfs.ext4 /dev/mmcblk0p3
mkdir -p /data
mount /dev/mmcblk0p3 /data
chmod 700 /data
```

Pour le **test actuel**, l'Option A suffit.

---

## Étape 7 — Configuration WiFi

Le fichier `/data/gygeslink/wifi.conf` est utilisé par `gygeslink-network-setup.sh`.

Crée-le avec les credentials de ton réseau :

```bash
mkdir -p /data/gygeslink
cat > /data/gygeslink/wifi.conf << 'EOF'
network={
    ssid="TON_SSID"
    psk="TON_MOT_DE_PASSE"
    key_mgmt=WPA-PSK
}
EOF
chmod 600 /data/gygeslink/wifi.conf
```

> **Sécurité** : ce fichier contient ton mot de passe WiFi en clair. Les permissions `600` le rendent lisible uniquement par root.

---

## Étape 8 — Configuration Tor (sans bridges pour le test)

Pour le premier test, on ne met pas encore de bridges obfs4 (on verifiera que Tor bootstrap sans).

Vérifie que `/etc/tor/torrc` est bien présent (copié depuis le repo). Si non :
```bash
cat > /etc/tor/torrc << 'EOF'
SocksPort 127.0.0.1:9050
TransPort 127.0.0.1:9040
DNSPort 127.0.0.1:5353
VirtualAddrNetworkIPv4 10.192.0.0/10
AutomapHostsOnResolve 1
RunAsDaemon 0
EOF
```

---

## Étape 9 — Activation des services systemd

Voici l'ordre d'activation. On ne démarre pas encore les services GygesLink — on les **active** juste pour qu'ils démarrent au boot.

```bash
# 1. Timesync (RTC DS3231 ou fallback systemd-timesyncd)
#    (Si tu n'as pas de DS3231, ce service peut échouer — c'est OK)
systemctl enable gygeslink-timesync.service

# 2. USB Gadget (crée usb0 via configfs)
systemctl enable gygeslink-usb-gadget.service

# 3. Configuration réseau (usb0, wlan0, iptables DROP, WiFi)
systemctl enable gygeslink-network-setup.service

# 4. Tor
systemctl enable gygeslink-tor.service

# 5. Ouverture du firewall (iptables-OPEN) — dépend de Tor
systemctl enable gygeslink-iptables-open.service

# 6. Jitter réseau (tc netem)
systemctl enable gygeslink-jitter.service

# 7. Noise daemon
systemctl enable gygeslink-noise.service

# 8. LED (optionnel, si pas de LEDs on masque — voir §10)
# systemctl enable gygeslink-led.service

# 9. Bouton (optionnel)
# systemctl enable gygeslink-button.service

# 10. Setup portal (démarrage conditionnel — ne l'active pas manuellement,
#     il est démarré par gygeslink-setup.service si setup-done est absent)
```

### Désactiver les conflits
```bash
# S'assurer que NetworkManager ne redémarre pas
systemctl disable NetworkManager 2>/dev/null || true
systemctl mask NetworkManager 2>/dev/null || true

# Désactiver dnsmasq par défaut (notre service le gère)
systemctl disable dnsmasq 2>/dev/null || true
```

---

## Étape 10 — Gestion des services optionnels (LED / Bouton)

Si ton boîtier n'a **pas encore** les LEDs et le bouton GPIO câblés, masque les services pour éviter les échecs au boot :

```bash
systemctl mask gygeslink-led.service
systemctl mask gygeslink-button.service
```

Quand tu câbleras les LEDs plus tard, tu pourras les démasquer :
```bash
systemctl unmask gygeslink-led.service
systemctl unmask gygeslink-button.service
```

---

## Étape 11 — Premier test sur hardware (alimentation secteur)

**Important** : reste connecté via le WiFi (SSH), le câble USB-C est encore sur l'alim secteur, pas sur le PC.

Redémarre le boîtier :
```bash
reboot
```

Attends ~30–60 secondes, puis reconnecte-toi en SSH.

### Vérifications post-boot

Exécute ces commandes une par une et note le résultat :

```bash
# 1. Modules kernel chargés ?
lsmod | grep -E "dwc2|libcomposite|usb_f_rndis"

# 2. Interface usb0 existe ?
ip link show usb0

# 3. Adresse IP usb0 correcte ?
ip addr show usb0
# Attendu : inet 192.168.100.1/24

# 4. configfs monté ?
mount | grep configfs

# 5. Gadget actif ?
ls /sys/kernel/config/usb_gadget/gygeslink/

# 6. iptables DROP actif ?
iptables -L -n -v | head -20
# Tu dois voir des règles DROP partout

# 7. Services OK ?
systemctl --failed
# Si gygeslink-timesync ou gygeslink-led sont failed, c'est probablement normal

# 8. Tor tourne ?
systemctl status gygeslink-tor.service
# Dans les logs, cherche "Bootstrapped 100% (done): Done"
```

---

## Étape 12 — Test sur PC Windows (moment de vérité)

1. **Éteins le boîtier proprement** :
   ```bash
   sudo shutdown now
   ```

2. **Débranche le câble d'alimentation secteur.**

3. **Branche le câble USB-C du boîtier directement à ton PC Windows.**
   - Le port USB-C de l'Orange Pi Zero 2W est OTG : il peut servir d'alimentation ET de données.
   - Si le PC ne délivre pas assez de courant, garde le câble secteur ET branche le câble USB-C au PC (mais normalement le PC alimente suffisamment).

4. **Allume/attends** que le boîtier démarre (~30–60s).

5. **Sur Windows, vérifie si une nouvelle carte réseau apparaît** :
   - `Win + R` → `ncpa.cpl` (Connexions réseau).
   - Regarde si tu vois **"GygesLink RNDIS Ethernet"** (ou un périphérique USB Ethernet).

6. **Si Windows ne reconnaît pas le périphérique** :
   - Vérifie dans le Gestionnaire de périphériques (`devmgmt.msc`).
   - S'il y a un point d'exclamation jaune sur "Unknown USB device", le driver RNDIS n'a pas été chargé automatiquement.
   - **Contournement** : installe le driver manuellement via Zadig ou via Device Manager → Mettre à jour le pilote → Rechercher "Remote NDIS based Internet Sharing Device".

7. **Si la carte réseau est bien présante** :
   - Elle devrait obtenir une IP en DHCP depuis dnsmasq (`192.168.100.100` par exemple).
   - Teste : `ping 192.168.100.1`
   - Essaye d'ouvrir `https://192.168.100.1` dans un navigateur (le setup portal Flask devrait répondre si setup-done est absent).

---

## Dépannage rapide

| Problème | Cause probable | Solution |
|---|---|---|
| `usb0` n'apparaît pas | `dwc2` pas chargé ou câble pas branché au boot | Vérifie `lsmod \| grep dwc2`, rebranche le câble, reboot |
| Windows ne voit rien | OS descriptors mal configurés | Vérifie les fichiers `os_desc/` dans configfs |
| `wlan0` n'obtient pas d'IP | WiFi mal configuré ou WPA2 erroné | Vérifie `/data/gygeslink/wifi.conf` et `wpa_cli -i wlan0 status` |
| Tor ne bootstrap pas | Pas d'accès internet ou bridges invalides | Vérifie `curl -x socks5h://127.0.0.1:9050 https://check.torproject.org` |
| `iptables-restore` échoue | Fichier `.rules` mal formaté | Vérifie les logs : `journalctl -u gygeslink-network-setup.service` |
| dnsmasq échoue | Port 53 déjà occupé | `systemctl stop systemd-resolved` ou vérifier que dnsmasq écoute sur `usb0` uniquement |
| Service `gygeslink-led` failed | Pas de LEDs câblées | `systemctl mask gygeslink-led.service` |
| Orange Pi ne démarre pas | Mauvaise image ou SD corrompue | Reflash, vérifier l'image |

---

## Prochaines étapes après ce test

1. **Si le gadget USB fonctionne** → on active les bridges Tor obfs4.
2. **Si Windows refuse RNDIS** → on documente le workaround Zadig et on teste en CLI/SSH (le portail n'est pas un blocker pour le MVP).
3. **Validation Tor** → `check.torproject.org` depuis le PC ; vérifier DNS leak ; Wireshark sur `wlan0`.
4. **Test fail-close** → `sudo systemctl stop gygeslink-tor` ; vérifier que le trafic est bloqué.
5. **WireGuard Tier 2** → si un voucher Mullvad est disponible.

---

## Récapitulatif des commandes post-boot (à garder sous la main)

```bash
# Statut global
systemctl status

# Logs d'un service
journalctl -u gygeslink-usb-gadget.service -f
journalctl -u gygeslink-network-setup.service -f
journalctl -u gygeslink-tor.service -f

# Vérifier Tor
curl --socks5 127.0.0.1:9050 https://check.torproject.org/api/ip

# Vérifier les interfaces
ip addr
ip route

# Vérifier iptables
iptables -t nat -L -n -v
iptables -L -n -v

# Redémarrer un service
systemctl restart gygeslink-usb-gadget.service
systemctl restart gygeslink-network-setup.service
```
