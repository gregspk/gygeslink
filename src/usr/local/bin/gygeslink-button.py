#!/usr/bin/env python3
"""
GygesLink — Daemon de surveillance du bouton GPIO (Orange Pi Zero 2W)

Câblage (Orange Pi Zero 2W — Allwinner H618, header 26-pin) :
  Vérification obligatoire avant déploiement :
    gpiodetect                          # lister les chips
    gpioinfo gpiochip0 | grep -i "PH"   # vérifier les lignes PH

  Bouton connecté entre Pin 7 (PH14) et GND (Pin 9).
  Pull-up interne activée : HIGH = relâché, LOW = pressé.
  Debounce logiciel : 50ms de stabilité requise.

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
# H618 GPIO Port H : base = 7 × 32 = 224 dans gpiod (gpiochip0)
# Header 26-pin → correspondances physiques :
#   Pin  7 = PH14 = ligne 224+14 = 238  → Bouton
#
# VALEURS PAR DÉFAUT — à confirmer avec `gpioinfo gpiochip0` sur le Pi.
# Si différentes, créer /data/gygeslink/gpio.conf :
#   GPIOCHIP=gpiochip0
#   BUTTON_LINE=238
GPIOCHIP    = "gpiochip0"
BUTTON_LINE = 238

GPIO_CONF_FILE  = Path("/data/gygeslink/gpio.conf")
HOLD_DURATION   = 5.0
DEBOUNCE_MS     = 0.05
POLL_INTERVAL   = 0.02
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


def _load_gpio_conf() -> None:
    global GPIOCHIP, BUTTON_LINE
    if not GPIO_CONF_FILE.exists():
        return
    mapping = {"GPIOCHIP": GPIOCHIP, "BUTTON_LINE": BUTTON_LINE}
    for line in GPIO_CONF_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in mapping:
            mapping[key] = int(val.strip()) if key != "GPIOCHIP" else val.strip()
    GPIOCHIP = mapping["GPIOCHIP"]
    BUTTON_LINE = mapping["BUTTON_LINE"]


def _debounced_read(line, expected: int, stability: float = DEBOUNCE_MS) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < stability:
        if line.get_value() != expected:
            return False
        time.sleep(0.005)
    return True

def trigger_setup_reset() -> None:
    """Supprime toute config utilisateur, redémarre en mode setup (factory reset)."""
    logger.warning("Maintien 5s détecté — factory reset.")

    files_to_delete = [
        SETUP_DONE_FILE,
        WIFI_CONF_FILE,
        Path("/etc/NetworkManager/system-connections/GygesLink-WiFi.nmconnection"),
        Path("/data/gygeslink/wg0.conf"),
        Path("/data/gygeslink/wg-expiry.txt"),
    ]

    for f in files_to_delete:
        if f.exists():
            try:
                f.unlink()
                logger.info("%s supprimé.", f)
            except OSError as e:
                logger.error("Impossible de supprimer %s : %s", f, e)

    # bridges.conf MUST exist (even empty) — torrc %include crashes if file absent
    bridges = Path("/data/gygeslink/bridges.conf")
    bridges.write_text("# GygesLink — Bridges obfs4\n")
    bridges.chmod(0o644)
    logger.info("bridges.conf réinitialisé (vide).")

    # Arrêter WireGuard si actif
    subprocess.run(["wg-quick", "down", "wg0"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
                time.sleep(POLL_INTERVAL)
                continue

            # ── Potentiel appui : vérifier avec debounce ──────────
            if not _debounced_read(line, 0):
                continue

            press_time = time.monotonic()
            logger.info("Bouton pressé, décompte %.0fs...", HOLD_DURATION)

            while True:
                if line.get_value() == 1:
                    if not _debounced_read(line, 1):
                        continue
                    held = time.monotonic() - press_time
                    logger.info("Bouton relâché après %.1fs — ignoré.", held)
                    break

                held = time.monotonic() - press_time
                if held >= HOLD_DURATION:
                    trigger_setup_reset()
                    logger.error("Reboot non déclenché — vérifier systemctl.")
                    return

                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Arrêt du daemon bouton.")
    finally:
        line.release()
        logger.info("GPIO libéré.")


# ─────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_gpio_conf()
    logger.info("GygesLink button daemon démarrage...")
    watch_button()


if __name__ == "__main__":
    main()
