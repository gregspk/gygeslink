# ⚡ GygesLink — Setup Rapide (post-reflash)

Ce guide est une **checklist technique** rapide pour valider la chaîne complète en 10 minutes.

## Prérequis avant de flasher

| Item | Valeur |
|---|---|
| Image | `Armbian_community_26.2.0-trunk.792_Orangepizero2w_trixie_current_6.18.24_minimal.img` |
| SD | ≥ 16 Go |
| Flash tool | Balena Etcher / Rufus |
| WiFi | SSID + mot de passe |

---

## 1. Préparer la SD avec WiFi préconfiguré

### 1.1 Flasher l'image sur la SD

### 1.2 Créer `wpa_supplicant.conf` sur la partition `bootfs`
Insère la SD sur ton PC Windows (Mac/Linux aussi).
Ouvre le lecteur nommé `bootfs` ou `BOOT`.
Crée un fichier nommé exactement `wpa_supplicant.conf` à la racine avec :

```
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=FR

network={
    ssid="Livebox-F430"
    psk="TON_MOT_DE_PASSE_WIFI"
    key_mgmt=WPA-PSK
}
```

> **Pourquoi ?** Cela évite d'avoir à taper interactivement le WiFi sur un petit écran clavier merdique. Le Pi se connectera seul au boot, tu pourras SSH directement.

### 1.3 Insérer la SD dans le Pi, brancher l'alimentation (secteur)
Attendre ~2 min le premier boot.

---

## 2. Premier boot — SSH immédiat

Trouve l'IP du Pi (interface routeur, scanner réseau, ou `nmap`).

```bash
ssh root@<IP>
```

Mot de passe Armbian par défaut : `1234`

---

## 3. Préparation système (en root sur le Pi)

```bash
# Mettre à jour
apt update && apt upgrade -y

# Installer les dépendances CRITIQUES
# (dhcpcd est OBLIGATOIRE, dhclient n'existe pas sur Armbian minimal)
apt install -y \
  iptables dnsmasq dhcpcd macchanger wpasupplicant \
  tor obfs4proxy wireguard-tools python3-pip python3-libgpiod \
  i2c-tools git
```

---

## 4. Déployer le code GygesLink

```bash
cd /opt
git clone https://github.com/gregspk/gygeslink.git
cd gygeslink

# Copier les fichiers système
cp -rv src/* /

# Permissions
chmod +x /usr/local/bin/gygeslink-*.sh
chmod +x /usr/local/bin/gygeslink-*.py
chmod +x /usr/local/bin/noise_generator.py
chmod +x /usr/local/bin/gygeslink-tor-prestart.sh
chmod +x /usr/local/bin/gygeslink-wireguard-check.sh
```

---

## 5. Créer la partition persistante `/data`

```bash
mkdir -p /data/gygeslink
chmod 700 /data/gygeslink
```

---

## 6. Configuration WiFi pour les boots futurs

Crée le fichier utilisé par `gygeslink-network-setup.sh` au boot :

```bash
cat > /data/gygeslink/wifi.conf << 'EOF'
network={
    ssid="Livebox-F430"
    psk="TON_MOT_DE_PASSE_WIFI"
    key_mgmt=WPA-PSK
}
EOF
chmod 600 /data/gygeslink/wifi.conf
```

---

## 7. Activer les services au boot

```bash
# Désactiver conflits éventuels
systemctl disable dnsmasq 2>/dev/null || true

# Services GygesLink
systemctl enable gygeslink-timesync.service     # peut fail sans DS3231, OK
systemctl enable gygeslink-usb-gadget.service   # crée usb0 (RNDIS)
systemctl enable gygeslink-network-setup.service  # wlan0 + iptables DROP
systemctl enable gygeslink-tor.service            # Tor
cystemctl enable gygeslink-iptables-open.service # iptables OPEN après Tor
systemctl enable gygeslink-jitter.service         # tc netem
systemctl enable gygeslink-noise.service          # bruit de fond

# Désactiver les services LED / button si non câblés
systemctl disable gygeslink-led.service     || true
systemctl disable gygeslink-button.service  || true
```

---

## 8. Mode Setup vs Mode Prod (CRITIQUE)

### 8.1 TEST 1 : Valider la chaîne technique (WiFi déjà configuré)
Tu veux que Tor bootstrapp et que tout fonctionne. Le setup portal n'est PAS nécessaire.

```bash
# Indiquer que le setup est déjà fait
touch /data/gygeslink/setup-done
```

### 8.2 TEST 2 : Valider le setup portal client
Tu veux que le portal sur `https://192.168.100.1` apparaisse quand un PC Windows se connecte.

```bash
# Retirer les flags setup et WiFi
rm -f /data/gygeslink/setup-done
rm -f /data/gygeslink/wifi.conf
```

> **Pour l'instant on fait le TEST 1.** Vérifie d'abord que la chaîne Tor fonctionne, le WiFi tient, et le firewall passe de DROP à OPEN. Le portal viendra après.

---

## 9. Reboot et vérifications SSH

```bash
reboot
```

Attends ~30–60s. Reconnecte-toi en SSH. Puis exécute :

```bash
# 1. Modules USB chargés ?
lsmod | grep -E 'dwc2|libcomposite|usb_f_rndis'

# 2. usb0 existe ?
ip link show usb0

# 3. usb0 a bien l'IP ?
ip addr show usb0 | grep 192.168.100.1

# 4. wlan0 a une IP ?
ip addr show wlan0 | grep 'inet '

# 5. iptables DROP est actif ?
iptables -L -v -n | head -10

# 6. Services en erreur ?
systemctl --failed

# 7. Tor a bootstrapé ?
journalctl -u gygeslink-tor.service | grep -i 'bootstrapped 100%'
```

**Si l'une de ces étapes échoue**, relis les logs pertinents :

```bash
journalctl -u gygeslink-network-setup.service   --no-pager
journalctl -u gygeslink-usb-gadget.service      --no-pager
journalctl -u gygeslink-tor.service             --no-pager
```

---

## 10. Test sur PC Windows (Test 1 validé)

1. Garde l'**alimentation secteur** branchée.
2. Branche le **câble USB-C OTG** (le deuxième port USB-C) à ton PC Windows.
3. Attends ~30s.
4. Sur Windows : `Win + R` → `ncpa.cpl`. Vérifie si une nouvelle carte réseau apparaît.
5. Essaye `ping 192.168.100.1`.

---

## Prochaines étapes après boot réussi

| Quand | Action |
|---|---|
| TEST 1 OK | On ajoute les bridges Tor obfs4. On édite `/etc/tor/torrc`. |
| TEST 1 OK + bridges OK | On efface `setup-done` et `wifi.conf` pour passer au TEST 2 (portal). |
| TEST 2 OK | On câble les LEDs + bouton GPIO pour les services respectifs. |
| LEDs câblées | `systemctl unmask gygeslink-led.service ; systemctl enable gygeslink-led.service` |
| Production | On active overlayfs en lecture seule pour hardening. |

---

## Raccourcis mémo

```bash
# Voir les logs d'un service
journalctl -u gygeslink-network-setup.service --no-pager

# Redémarrer un service
systemctl restart gygeslink-network-setup.service

# Vérifier Tor
curl --socks5 127.0.0.1:9050 https://check.torproject.org/api/ip

# Voir toutes les interfaces
ip addr

# Voir les règles NAT
iptables -t nat -L -n -v
```
