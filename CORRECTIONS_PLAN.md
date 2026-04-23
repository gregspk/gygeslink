# GygesLink — Plan de corrections
## Date : 2026-04-23
## Audit : OpenCode (kimik-k2.6)

Ce plan reprend l'analyse complète du codebase actuel de GygesLink ainsi que la compréhension du projet via les fichiers de spécification et des plans d'implémentation. Chaque anomalie est classée par sévérité avec son contexte, son impact et les modifications exactes à effectuer.

---

## Légende
| Sévérité | Signification |
|---|---|
| 🔴 CRITIQUE | Bloquant pour le fonctionnement ou la sécurité — doit être corrigé avant tout test sur hardware |
| 🟠 IMPORTANT | Fonctionnellement incorrect ou risque de bypass/regression — à corriger avant déploiement |
| 🟡 MINEUR | Amélioration de robustesse, propreté ou maintainabilité — à corriger idéalement avant go-live |

---

## 🔴 CRITIQUE

### C-01 – Portail setup inaccessible au premier boot (iptables DROP bloque usb0→443)
**Fichier(s) :** `gygeslink-network-setup.sh` (nouvelle branche setup) + `gygeslink-setup.service` (clean-up règles temporaires)
**Référence(s) :** `CLAUDE.md` §« Portail setup » ; `iptables-drop.rules` (bloque tout sauf DHCP)

**Description :**
En premier boot (`setup-done` absent), `wifi.conf` n'existe pas. `network-setup.sh` échoue l’étape WiFi, sort avec `exit 0` et laisse le piège fail-close en place. Le portail Flask démarre bien sur `192.168.100.1:443`, mais `iptables-drop.rules` n’autorise aucune connexion entrante sur `usb0` sauf le serveur DHCP (UDP/67). Le PC obtient une IP mais **ne peut pas joindre le portail HTTPS**. Le setup devient fonctionnellement impossible.

**Impact :** Premier boot bloqué — l’utilisateur ne peut jamais configurer le WiFi ni choisir le tier.

**Correction proposée :**
1. Dans `gygeslink-network-setup.sh`, après avoir appliqué `iptables-drop.rules`, vérifier si `setup-done` est absent.
2. Si setup actif, ajouter **temporairement** les règles iptables nécessaires au portail :
   - `INPUT -i usb0 -p tcp --dport 443 -j ACCEPT`
   - `OUTPUT -o usb0 -p tcp --sport 443 -j ACCEPT`
3. (Optionnel mais propre) Autoriser aussi le trafic HTTP 80 pour la redirection automatique vers 443, ou rediriger en interne sans exposer 80. Le plus simple :
   - Dans `network-setup.sh` : après restore des règles DROP, si pas de `setup-done`, injecter directement avec `iptables -I INPUT -i usb0 -p tcp --dport 443 -j ACCEPT` et `iptables -I OUTPUT -o usb0 -p tcp --sport 443 -j ACCEPT`.
4. Dans `gygeslink-setup.service`, le `ExecStopPost` actuel supprime les règles spécifiques du setup. Il faut l’élargir pour qu’il nettoie **aussi** ces règles temporaires injectées par `network-setup.sh`.

**Extrait de correction cible (network-setup.sh) :**
```bash
# Si setup non terminé, ouvrir le portail temporairement
if [ ! -f /data/gygeslink/setup-done ]; then
    LOG "Mode setup détecté : ouverture temporaire du portail HTTPS sur usb0."
    iptables -I INPUT -i usb0 -p tcp --dport 443 -j ACCEPT
    iptables -I OUTPUT -o usb0 -p tcp --sport 443 -j ACCEPT
fi
```

**Extrait de correction cible (gygeslink-setup.service ExecStopPost) :**
```bash
ExecStopPost=/bin/sh -c 'iptables-save | grep -q "dport 443" && iptables -D INPUT -i usb0 -p tcp --dport 443 -j ACCEPT 2>/dev/null || true'
ExecStopPost=/bin/sh -c 'iptables-save | grep -q "sport 443" && iptables -D OUTPUT -o usb0 -p tcp --sport 443 -j ACCEPT 2>/dev/null || true'
```

---

### C-02 – LED : vérification erronée du service iptables-open au lieu de Tor
**Fichier(s) :** `gygeslink-led.py`

**Description :**
`get_system_state()` teste `gygeslink-iptables-open` pour décider si le trafic est protégé. Or ce service est `Type=oneshot` avec `RemainAfterExit=yes` → il reste **actif pour systemd même si Tor s’est crashé ensuite** (ex: bridge obfs4 invalide, OOM, restart failure). La LED ne bascule donc pas à rouge si Tor meurt après le boot initial.

**Impact :** LED trompeuse — l’utilisateur pense être protégé alors que Tor est mort et le trafic est redirigé depuis longtemps vers un service inactif (= potentiellement bloqué, mais l’état affiché est faux).

**Correction proposée :**
Modifier `get_system_state()` pour tester `gygeslink-tor.service` en **priorité** (ou en combinaison). Exemple :
```python
# Si setup done mais Tor est KO → erreur critique (rouge clignotant)
if not service_active("gygeslink-tor"):
    return "error"
```
Ou, plus prudemment, garder le test sur `gygeslink-iptables-open` **ET** ajouter le test sur Tor. Si l’un est inactif → rouge.

---

## 🟠 IMPORTANT

### I-01 – Endpoint API Mullvad incorrect (vérifier/corriger)
**Fichier(s) :** `gygeslink-setup-portal.py` (ligne ~56, fonction `register_wireguard_key`)

**Description :**
Le code appelle `https://api.mullvad.net/wg/` en POST avec `account` + `pubkey`. L’API réelle de Mullvad a évolué et l’endpoint pour enregistrer une clé WireGuard est généralement `https://api.mullvad.net/app/v1/wg-keys` (ou similaire), avec une authentification différente (Bearer token ou compte). L’endpoint actuel risque de renvoyer 400/404, ce qui rend la configuration Tier 2 impossible via le portail.

**Impact :** Tier 2 impossible à configurer en mode setup utilisateur.

**Correction proposée :**
1. Consulter la documentation actuelle de l’API Mullvad (vérifier l’endpoint exact et le format de requête).
2. Adapter `register_wireguard_key(account, public_key)` avec :
   - L’URL exacte (ex: `https://api.mullvad.net/app/v1/wg-keys`).
   - Les bons headers d’authentification si nécessaire.
   - Le parsing correct de la réponse JSON (champs `ipv4_address`, `pubkey`, `expires`, etc.).
3. Assurer la validation stricte des champs retournés (déjà partiellement fait avec `ipaddress` et regex pubkey).

---

### I-02 – Manque `src_valid_mark=1` pour WireGuard
**Fichier(s) :** `gygeslink-wireguard-check.sh`

**Description :**
`wg-quick` a besoin de `net.ipv4.conf.all.src_valid_mark=1` pour fonctionner correctement dans un contexte de policy routing avancé (quand Tor doit forcer sa sortie via `wg0` avec `OutboundBindInterface`). Sans ce sysctl, le tunnel WireGuard peut être monté mais Tor ne parvient pas à router le trafic dedans.

**Impact :** Tier 2 monté mais Tor ne passe pas par le tunnel → chute implicite au réseau direct sans fallback propre, ou timeout silencieux.

**Correction proposée :**
Ajouter dans `gygeslink-wireguard-check.sh`, avant `wg-quick up wg0` :
```bash
sysctl -w net.ipv4.conf.all.src_valid_mark=1 > /dev/null 2>&1 || true
```
Et éventuellement persister dans `99-gygeslink.conf` :
```
net.ipv4.conf.all.src_valid_mark = 1
```

---

### I-03 – `exit 0` masque les erreurs réseau dans `network-setup.sh`
**Fichier(s) :** `gygeslink-network-setup.sh` (lignes 115, 124, 141, 151)

**Description :**
Plusieurs branches d’erreur critiques retournent `exit 0` (ex: absence de `wifi.conf`, timeout WiFi, timeout DHCP). Avec `Type=oneshot`, systemd marque le service comme **succès**, ce qui permet à `gygeslink-tor.service` de démarrer ensuite. Tor va alors timeout sur le bootstrap (pas de connectivité), mais la logique fail-close n’est **pas** mise en œuvre explicitement (on laisse Tor planter plutôt que de bloquer sainement).

**Impact :** Séquence boot incorrecte — Tor démarre sans réseau, consomme du temps CPU, et la LED risque de rester sur le dernier état disponible.

**Correction proposée :**
Remplacer les `exit 0` de branches critiques par `exit 1` :
- `wifi.conf` absent : `exit 1` (laisse le setup service prendre le relais, mais network-setup doit être failed)
- timeout WiFi (`WAITED >= WIFI_TIMEOUT`) : `exit 1`
- timeout DHCP : `exit 1`
- échec `wpa_supplicant` : `exit 1`

Adapter éventuellement `gygeslink-tor.service` avec `ConditionPathExists=/data/gygeslink/wifi.conf` si l’on souhaite éviter que Tor tente de démarrer sans WiFi configuré.

---

### I-04 – `tc qdisc add` non idempotent (redémarrage de jitter KO)
**Fichier(s) :** `gygeslink-jitter.service`

**Description :**
`ExecStart=/sbin/tc qdisc add dev wlan0 root netem ...` échoue si une qdisc existe déjà sur `wlan0` (ex: redémarrage manuel ou partiel du service, suite à un `systemctl restart`). L’erreur est silencieuse mais le service est marqué failed.

**Impact :** Jitter non appliqué après un redémarrage partiel du service.

**Correction proposée :**
Remplacer `add` par `replace` (idempotent) :
```
ExecStart=/sbin/tc qdisc replace dev wlan0 root netem delay 20ms 15ms distribution pareto
```
Nettoyage identique (`del` reste bien car `replace` ne vire pas à l’arrêt).

---

### I-05 – Validation partielle des bridges obfs4 (placeholders laissés)
**Fichier(s) :** `gygeslink-tor-prestart.sh` (fonction bridge validation)

**Description :**
Le script check uniquement s’il y a "au moins une" ligne `Bridge obfs4 [0-9]` valide. Si l’utilisateur ne remplace qu’une seule bridge parmi les trois placeholders, Tor recevra 3 lignes `Bridge` dont 2 invalides. Tor refusera de démarrer.

**Impact :** Tor fail au boot sans message explicite si seulement une partie des bridges a été remplacée.

**Correction proposée :**
Vérifier que **toutes** les lignes commençant par `Bridge` sont valides, et qu’aucune ne contient `REMPLACER`. Exemple :
```bash
# Rejeter si une ligne Bridge contient un placeholder
if grep -qE "^Bridge .*REMPLACER" "$BRIDGES_CONF" 2>/dev/null; then
    HAS_VALID_BRIDGES=0
fi
```

---

## 🟡 MINEUR

### M-01 – `noise_generator.py` gère mal `SIGTERM`
**Fichier(s) :** `noise_generator.py`

**Description :**
Le daemon Python utilise `asyncio.run()` sans gestionnaire de signal `SIGTERM`. À l’arrêt du service (`systemctl stop gygeslink-noise`), le processus est tué brutalement, les `ClientSession`/`ProxyConnector` ne sont pas fermés, ce qui peut laisser des sockets en état `TIME_WAIT`.

**Correction proposée :**
Installer un gestionnaire `signal.SIGTERM` qui annule une `asyncio.Event` pour sortir proprement de la boucle principale, puis `await session.close()` / `connector.close()`. Ou utiliser `asyncio.get_running_loop().add_signal_handler`.

---

### M-02 – `network.conf` : commentaire trompeur sur "sourced directement"
**Fichier(s) :** `src/data/gygeslink/network.conf`

**Description :**
Le commentaire dit "FORMAT : variable=valeur (syntaxe bash, sourced directement)". En réalité, `gygeslink-network-setup.sh` le parse ligne par ligne avec une whitelist (`case`) — il n’est **pas** sourcé.

**Correction proposée :**
Corriger le commentaire en :
```
# FORMAT : variable=valeur (parsé manuellement par gygeslink-network-setup.sh
# avec une whitelist — NE JAMAIS source ce fichier).
```

---

### M-03 – `file_list.txt` obsolète (références à fichiers supprimés)
**Fichier(s) :** `file_list.txt`

**Description :**
Le fichier liste encore :
- `src/etc/dnsmasq.d/gygeslink-setup.conf` (supprimé)
- `src/etc/hostapd/hostapd.conf` (supprimé)
- `src/etc/udev/rules.d/10-gygeslink-net.rules` (supprimé)

**Correction proposée :**
Regénérer la liste réelle (`find src -type f | sort`) ou supprimer le fichier (il n’est pas référencé par le plan d’implémentation).

---

### M-04 – Debounce manquant sur le bouton GPIO
**Fichier(s) :** `gygeslink-button.py`

**Description :**
Un bouton mécanique sans hardware debounce va rebondir. À 100ms de polling, un rebond peut être comptabilisé comme un appui court ou, pire, fausser la mesure des 5 secondes.

**Correction proposée :**
Ajouter une logique software debounce : exiger que le bouton reste LOW pendant au moins 50–100ms consécutifs avant de considérer l’appui comme réel. Ou utiliser libgpiod avec détection d’edge + callback.

---

### M-05 – `sysctl nf_conntrack` sans chargement du module
**Fichier(s) :** `99-gygeslink.conf`

**Description :**
Le fichier définit `net.netfilter.nf_conntrack_tcp_timeout_established = 600`, mais si le module `nf_conntrack` n’est pas encore chargé au moment où systemd-sysctl lit le fichier, le paramètre n’est pas appliqué.

**Correction proposée :**
Ajouter un `modprobe nf_conntrack` dans `gygeslink-network-setup.sh` (ou un drop-in `modules-load.d`) avant `sysctl -p`, ou vérifier à l’installation que le module est chargé.

---

### M-06 – `sysctl` runtime vs fichier : divergence potentielle
**Fichier(s) :** `99-gygeslink.conf`, `gygeslink-network-setup.sh`

**Description :**
Plusieurs paramètres sysctl sont appliqués à la fois dans le fichier `.conf` (boot) et dans le script shell (runtime). C’est volontaire, mais si on modifie le `.conf` sans modifier le script, les valeurs divergent. Ex: `net.ipv6.conf.default.disable_ipv6=1` est absent de `99-gygeslink.conf` (seul `all` et `lo` sont définis) alors que le script applique aussi `default`.

**Correction proposée :**
S’assurer que `99-gygeslink.conf` contient **exactement** les mêmes clés que le script : `all` + `default` + `lo`. Ou mieux, dans le script, appeler `sysctl --system` (ou `sysctl -p /etc/sysctl.d/99-gygeslink.conf`) plutôt que répéter les valeurs à la main.

---

## Récapitulatif d’action immédiate (avant test hardware)

Avant de flasher le Orange Pi et de tester, les corrections suivantes doivent être appliquées obligatoirement :

1. ✅ C-01 : Ouvrir le portail en mode setup (`usb0` TCP/443 temporaire).
2. ✅ C-02 : Corriger la détection LED (vérifier Tor, pas seulement iptables-open).
3. ✅ I-01 : Vérifier/corriger l’endpoint API Mullvad (indispensable pour le Tier 2).
4. ✅ I-02 : Ajouter `src_valid_mark=1` pour WireGuard.
5. ✅ I-03 : Faire échouer `network-setup.service` en cas d’erreur réseau critique.
6. ✅ I-04 : Utiliser `tc qdisc replace` au lieu de `add`.
7. ✅ I-05 : Sanitizer les placeholders `Bridge` (rejeter si un placeholder subsiste).

Les items M-01 à M-06 peuvent être traités après validation hardware si nécessaire, mais leur correction reste recommandée.

---

## Questions ouvertes (à confirmer par l’utilisateur)

- **Bridge par défaut :** Le fichier `bridges.conf` contient 3 placeholders. L’idéal serait d’en fournir au moins 1 valide (avec un mécanisme de rotation) ou d’intégrer un helper qui va chercher bridges.torproject.org automatiquement au premier boot. À confirmer si le setup wizard doit générer les bridges ou si l’utilisateur doit le faire manuellement.
- **Endpoint Mullvad :** Doit-on intégrer un mécanisme de retry si le voucher est temporairement invalide (réseau instable) ou considérer tout 4xx comme définitif ?
- **LED et états partiels :** Si seulement le jitter échoue (ex: tc pas supporté par le kernel), est-ce que la LED doit passer orange (protection partielle) ou rouge (erreur critique) ? Actuellement elle passe orange — à confirmer.
