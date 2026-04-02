#!/usr/bin/env python3
"""
GygesLink — Noise daemon (générateur de trafic leurre)

Envoie des requêtes HTTPS vers des sites courants à intervalles aléatoires,
exclusivement via le SocksPort de Tor (127.0.0.1:9050).

Objectif : perturber l'analyse comportementale du trafic en rendant
impossible la distinction entre le "vrai" trafic et le bruit de fond.

Sécurité :
  - Tourne sous l'utilisateur gygeslink-noise (non root, non debian-tor)
  - iptables bloque tout OUTPUT de cet utilisateur sauf vers 127.0.0.1:9050
  - aiohttp-socks lève une exception si le proxy est injoignable —
    il ne fait JAMAIS de fallback vers une connexion directe

Démarré par gygeslink-noise.service, après gygeslink-iptables-open.service.
"""

import asyncio
import logging
import random
import sys
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyConnectionError

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

TOR_SOCKS_PROXY = "socks5://127.0.0.1:9050"

# Intervalle entre deux requêtes (secondes)
INTERVAL_MIN = 2.0
INTERVAL_MAX = 12.0

# Nombre maximum de requêtes simultanées
# Évite de surcharger Tor avec trop de circuits parallèles
MAX_CONCURRENT = 3

# Timeout par requête (secondes)
REQUEST_TIMEOUT = 20

# Sites vers lesquels envoyer du trafic leurre.
# Choisis pour leur fréquence d'accès "normale" sur internet.
# Aucun site sensible ou identifiant — le but est de ressembler
# à une navigation ordinaire.
NOISE_TARGETS = [
    "https://www.wikipedia.org",
    "https://en.wikipedia.org/wiki/Main_Page",
    "https://www.bbc.com",
    "https://www.reuters.com",
    "https://www.mozilla.org",
    "https://www.python.org",
    "https://www.debian.org",
    "https://news.ycombinator.com",
    "https://www.eff.org",
    "https://www.torproject.org",
    "https://www.archive.org",
    "https://www.gutenberg.org",
]

# ─────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [noise] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("noise")


# ─────────────────────────────────────────────────────────────────────
# Requête leurre
# ─────────────────────────────────────────────────────────────────────

async def fetch_noise(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
) -> None:
    """
    Envoie une requête GET vers url via le proxy Tor.
    Lit et jette le contenu (on ne veut que le trafic réseau).
    Les erreurs sont loguées en DEBUG et ignorées — une requête ratée
    n'est pas un problème, le daemon continuera.
    """
    async with semaphore:
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with session.get(url, timeout=timeout) as response:
                # Lire le corps pour générer du trafic réseau complet
                # (pas seulement les en-têtes)
                await response.read()
                logger.debug("OK %d %s", response.status, url)

        except ProxyConnectionError:
            # Le proxy Tor est injoignable — Tor n'est peut-être pas encore
            # complètement démarré. On attend, on ne panic pas.
            logger.debug("Proxy Tor injoignable pour %s — attente.", url)

        except aiohttp.ClientError as e:
            # Erreur réseau ordinaire (timeout, connexion refusée, etc.)
            logger.debug("Erreur réseau %s : %s", url, type(e).__name__)

        except asyncio.TimeoutError:
            logger.debug("Timeout %s", url)

        except Exception as e:  # noqa: BLE001
            # Toute autre erreur inattendue — on logue et on continue
            logger.debug("Erreur inattendue %s : %s", url, e)


# ─────────────────────────────────────────────────────────────────────
# Boucle principale
# ─────────────────────────────────────────────────────────────────────

async def noise_loop() -> None:
    """
    Boucle infinie qui envoie du trafic leurre à intervalles aléatoires.

    Utilise ProxyConnector de aiohttp-socks pour forcer le passage
    par Tor. Si le proxy est injoignable, les requêtes échouent —
    il n'y a JAMAIS de fallback vers une connexion directe.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    # ProxyConnector force toutes les connexions via SOCKS5 (Tor).
    # rdns=True : la résolution DNS est faite par Tor, pas localement.
    # Cela évite les fuites DNS même si une requête contourne le proxy.
    connector = ProxyConnector.from_url(TOR_SOCKS_PROXY, rdns=True)

    async with aiohttp.ClientSession(connector=connector) as session:
        logger.info("Noise daemon démarré. Proxy : %s", TOR_SOCKS_PROXY)
        logger.info(
            "Intervalle : %.0f–%.0fs | Concurrence max : %d",
            INTERVAL_MIN,
            INTERVAL_MAX,
            MAX_CONCURRENT,
        )

        while True:
            url = random.choice(NOISE_TARGETS)

            # Lancer la requête sans attendre sa fin
            # (on enchaîne les requêtes sans se bloquer)
            asyncio.create_task(fetch_noise(session, url, semaphore))

            # Attendre un intervalle aléatoire avant la prochaine requête
            delay = random.uniform(INTERVAL_MIN, INTERVAL_MAX)
            await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Point d'entrée du daemon.
    Lance la boucle asyncio et gère l'arrêt propre (SIGTERM/SIGINT).
    """
    logger.info("GygesLink noise daemon démarrage...")

    try:
        asyncio.run(noise_loop())
    except KeyboardInterrupt:
        logger.info("Arrêt du noise daemon (interruption manuelle).")
    except Exception as e:
        logger.error("Erreur fatale : %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
