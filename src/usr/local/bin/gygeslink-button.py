#!/usr/bin/env python3
"""
GygesLink — Daemon de surveillance du bouton GPIO (Orange Pi Zero 2W)

Câblage :
  TODO: vérifier le numéro de ligne gpiod avec `gpioinfo` sur le boîtier
  Commandes de vérification :
    gpiodetect           # lister les chips disponibles
    gpioinfo gpiochip0   # lister toutes les lignes

  Bouton connecté entre le pin GPIO (Pin 7, ligne 15) et GND.
  HIGH = bouton relâché, LOW = bouton pressé.

Comportement :
  Maintien 5 secondes → supprime /data/gygeslink/setup-done → reboot
  Le boîtier redémarre en mode setup.

Cas d'usage :
  - Changer de tier (Classic ↔ Advanced)
  - Reconfigurer les credentials WiFi
  - Reconfigurer le compte Mullvad
  - Réinitialisation après une mauvaise config
"""

import logging
import subprocess
import sys
import time
from pathlib import Path

try:
    import gpiod
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────
# Configuration GPIO (Orange Pi Zero 2W — Allwinner H618)
# ─────────────────────────────────────────────────────────────────────
# Orange Pi Zero 2W (Allwinner H618) — gpiochip1 = 288 lignes
# Pinout GPIO physique : broches du header 26-pin
#   Pin  7 (GPIO_X_6) = ligne 15  → Bouton
GPIOCHIP    = "gpiochip1"
BUTTON_LINE = 15

HOLD_DURATION   = 5.0   # secondes de maintien pour déclencher le reset
POLL_INTERVAL   = 0.1   # intervalle de polling (100ms)
SETUP_DONE_FILE = Path("/data/gygeslink/setup-done")

# ─────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [button] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("button")


# ─────────────────────────────────────────────────────────────────────
# Logique bouton
# ─────────────────────────────────────────────────────────────────────

WIFI_CONF_FILE = Path("/data/gygeslink/wifi.conf")

def trigger_setup_reset() -> None:
    """Supprime le flag setup-done et les credentials WiFi, redémarre en mode setup."""
    logger.warning("Maintien 5s détecté — reset vers mode setup.")

    if SETUP_DONE_FILE.exists():
        try:
            SETUP_DONE_FILE.unlink()
            logger.info("setup-done supprimé.")
        except OSError as e:
            logger.error("Impossible de supprimer setup-done : %s", e)
    else:
        logger.info("setup-done déjà absent.")

    if WIFI_CONF_FILE.exists():
        try:
            WIFI_CONF_FILE.unlink()
            logger.info("wifi.conf supprimé — reconfiguration WiFi requise.")
        except OSError as e:
            logger.error("Impossible de supprimer wifi.conf : %s", e)

    logger.info("Reboot dans 1 seconde...")
    time.sleep(1)
    subprocess.run(["systemctl", "reboot"], check=False)


def watch_button() -> None:
    """Boucle principale : surveille le bouton GPIO en polling."""
    if not GPIO_AVAILABLE:
        logger.warning("gpiod non disponible — bouton désactivé.")
        logger.warning("Installer : apt install python3-libgpiod")
        while True:
            time.sleep(60)

    chip = gpiod.Chip(GPIOCHIP)
    line = chip.get_line(BUTTON_LINE)

    # Pull-up interne : HIGH = relâché, LOW = pressé
    line.request(
        consumer="gygeslink-button",
        type=gpiod.LINE_REQ_DIR_IN,
        flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
    )

    logger.info(
        "Surveillance bouton %s ligne %d. Maintien %ds → reset setup.",
        GPIOCHIP, BUTTON_LINE, int(HOLD_DURATION),
    )

    try:
        while True:
            if line.get_value() == 1:
                # Bouton relâché (pull-up HIGH)
                time.sleep(POLL_INTERVAL)
                continue

            # ── Bouton pressé (LOW) ───────────────────────────────────
            press_time = time.time()
            logger.debug("Bouton pressé, décompte 5s...")

            while line.get_value() == 0:
                held = time.time() - press_time

                if held >= HOLD_DURATION:
                    trigger_setup_reset()
                    # trigger_setup_reset() reboot — on n'arrive pas ici
                    logger.error("Reboot non déclenché — vérifier systemctl.")
                    return

                time.sleep(POLL_INTERVAL)

            # Bouton relâché avant 5s
            held = time.time() - press_time
            if held >= 0.05:
                logger.debug("Appui court (%.1fs) — ignoré.", held)

    except KeyboardInterrupt:
        logger.info("Arrêt du daemon bouton.")
    finally:
        line.release()
        logger.info("GPIO libéré.")


# ─────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("GygesLink button daemon démarrage...")
    watch_button()


if __name__ == "__main__":
    main()
