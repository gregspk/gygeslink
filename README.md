# GygesLink

Boîtier physique plug-and-play qui anonymise le trafic réseau de votre PC via Tor + obfs4. Se branche en USB-C et se connecte au routeur en WiFi, aucune configuration réseau requise.

## Comment ça marche

```
[PC] ── USB-C ──> [GygesLink] ── WiFi ──> [Box FAI] ──> Internet
```

Le PC voit le boîtier comme une carte réseau USB. Tout le trafic TCP/DNS passe automatiquement par Tor avant de sortir sur le réseau, transparent pour l'utilisateur.

### Deux niveaux de protection

| Tier | Protection |
|---|---|
| **Phantom** | Tor + obfs4 + jitter temporel + bruit de fond |
| **Wraith** | Mullvad WireGuard + Tor + obfs4 + jitter + bruit de fond |

Le tier est détecté automatiquement au boot. Si un abonnement Mullvad expire, le boîtier bascule sur Phantom, jamais de coupure totale.

### Les couches

1. **Tor + obfs4** : anonymisation + obfuscation DPI (le trafic ressemble à du TLS aléatoire)
2. **WireGuard Mullvad** (Tier 2) : le FAI voit uniquement du trafic WireGuard, pas de signature Tor
3. **Jitter temporel** : délai aléatoire par paquet (tc netem, distribution Pareto) contre la corrélation temporelle
4. **Bruit de fond** : requêtes HTTPS aléatoires vers domaines courants via Tor pour masquer les métadonnées

### Fail-close

Les règles iptables DROP s'appliquent avant que le réseau soit disponible. Si Tor ne démarre pas, le trafic reste bloqué, il ne sort jamais en clair.

## Matériel

- Orange Pi Zero 2W 1GB (WiFi intégré + USB-C OTG)
- Module RTC DS3231 (horloge sans réseau au boot)
- LED RGB + bouton poussoir
- Boîtier imprimé 3D

## Premier démarrage

1. Brancher le boîtier en USB-C sur le PC → LED bleue clignotante
2. Ouvrir `https://192.168.100.1` depuis le PC
3. Saisir le SSID + mot de passe WiFi du routeur
4. Choisir Phantom ou Wraith (voucher Mullvad requis pour Advanced)
5. Reboot automatique → LED verte = protection active

Le bouton physique (maintien 5s) remet le boîtier en mode setup (changement de réseau, voucher, etc.).

## LEDs

| Couleur | État |
|---|---|
| Bleu clignotant | Mode setup |
| Vert fixe | Protection complète |
| Orange fixe | Protection partielle (Tor OK, couches avancées indisponibles) |
| Orange clignotant | Voucher Mullvad expire dans < 7 jours |
| Rouge clignotant | Erreur critique - trafic bloqué |

## Modèle de menace

**Protège contre :** surveillance FAI, DPI gouvernemental ciblant Tor, analyse de métadonnées, corrélation temporelle basique.

**Hors périmètre :** adversaire global, compromission physique du boîtier, sécurité du PC en amont, website fingerprinting ML avancé.

## Stack technique

Armbian (Debian 12) · Tor >= 0.4.8 · obfs4proxy · WireGuard · iptables · tc netem · Python 3 asyncio · Flask · systemd
