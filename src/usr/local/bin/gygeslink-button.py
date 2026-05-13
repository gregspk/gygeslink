#!/usr/bin/env python3
"""
GygesLink — Daemon de surveillance du bouton GPIO (Orange Pi Zero 2W)

Câblage (Orange Pi Zero 2W — Allwinner H618, header 26-pin) :
  Bouton connecté entre Pin 7 (PI13/GPIO 269) et GND (Pin 9).
  Pull-up interne activée : HIGH = relâché, LOW = pressé.
  Debounce logiciel : 50ms de stabilité requise.

Comportement :
  Maintien 5 secondes  → suspend-to-RAM (veille, ~0.5W)
  Maintien 20 secondes → factory reset (supprime config + reboot)

Wake from suspend :
  Le bouton GPIO est configuré comme wake-up source via sysfs.
  Un appui bref pendant la veille réveille le Pi.

Utilise sysfs (/sys/class/gpio) pour le contrôle GPIO — compatible
avec tous les kernels, sans dépendance à une version de libgpiod.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path

GPIOCHIP    = "/dev/gpiochip1"
BUTTON_LINE = 269

GPIO_CONF_FILE  = Path("/data/gygeslink/gpio.conf")
HOLD_DURATION_SUSPEND = 5.0
HOLD_DURATION_RESET   = 20.0
DEBOUNCE_MS           = 0.05
POLL_INTERVAL         = 0.02
SETUP_DONE_FILE = Path("/data/gygeslink/setup-done")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [button] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("button")

WIFI_CONF_FILE = Path("/data/gygeslink/wifi.conf")


def _load_gpio_conf() -> None:
    global BUTTON_LINE
    if not GPIO_CONF_FILE.exists():
        return
    mapping = {"BUTTON_LINE": BUTTON_LINE}
    for line in GPIO_CONF_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in mapping:
            mapping[key] = int(val.strip())
    BUTTON_LINE = mapping["BUTTON_LINE"]


def _gpio_write(path: str, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


def _gpio_export(pin: int) -> None:
    gpio_path = Path(f"/sys/class/gpio/gpio{pin}")
    if gpio_path.exists():
        return
    for attempt in range(5):
        try:
            _gpio_write("/sys/class/gpio/export", str(pin))
            return
        except OSError:
            time.sleep(0.1)
    if not gpio_path.exists():
        raise RuntimeError(f"Cannot export GPIO {pin} after 5 attempts")


def _gpio_enable_wakeup(pin: int) -> None:
    """Configurer le GPIO comme source de réveil du suspend-to-RAM."""
    wakeup_path = Path(f"/sys/class/gpio/gpio{pin}/device/power/wakeup")
    try:
        if wakeup_path.exists():
            wakeup_path.write_text("enabled")
            logger.info("GPIO %d configuré comme wake-up source.", pin)
    except OSError as e:
        logger.warning("Impossible d'activer le wake-up sur GPIO %d : %s", pin, e)


def _gpio_unexport(pin: int) -> None:
    gpio_path = Path(f"/sys/class/gpio/gpio{pin}")
    if gpio_path.exists():
        try:
            _gpio_write("/sys/class/gpio/unexport", str(pin))
        except OSError:
            pass


def _gpio_set_direction(pin: int, direction: str) -> None:
    _gpio_write(f"/sys/class/gpio/gpio{pin}/direction", direction)


def _gpio_get_value(pin: int) -> int:
    with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
        return int(f.read().strip())


def _debounced_read(pin: int, expected: int, stability: float = DEBOUNCE_MS) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < stability:
        if _gpio_get_value(pin) != expected:
            return False
        time.sleep(0.005)
    return True


def trigger_suspend() -> None:
    """Suspend-to-RAM : veille à faible consommation, réveil via bouton."""
    logger.warning("Maintien %.0fs détecté — suspend.", HOLD_DURATION_SUSPEND)
    subprocess.run(["systemctl", "suspend"], check=False)


def trigger_factory_reset() -> None:
    """Factory reset : supprime toute config utilisateur et redémarre en mode setup."""
    logger.warning("Maintien %.0fs détecté — factory reset.", HOLD_DURATION_RESET)

    files_to_delete = [
        SETUP_DONE_FILE,
        WIFI_CONF_FILE,
        Path("/etc/netplan/30-wifis-dhcp.yaml"),
        Path("/data/gygeslink/wg0.conf"),
        Path("/data/gygeslink/wg-expiry.txt"),
        Path("/data/gygeslink/paused"),
    ]

    for f in files_to_delete:
        if f.exists():
            try:
                f.unlink()
                logger.info("%s supprimé.", f)
            except OSError as e:
                logger.error("Impossible de supprimer %s : %s", f, e)

    bridges = Path("/data/gygeslink/bridges.conf")
    bridges.write_text("# GygesLink — Bridges obfs4\n")
    bridges.chmod(0o644)
    logger.info("bridges.conf réinitialisé (vide).")

    subprocess.run(["wg-quick", "down", "wg0"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    logger.info("Reboot dans 1 seconde...")
    time.sleep(1)
    subprocess.run(["systemctl", "reboot"], check=False)


def watch_button() -> None:
    """Boucle principale : surveille le bouton GPIO en polling via sysfs.

    Comportements :
      - Maintien 5s  → suspend-to-RAM (veille)
      - Maintien 20s → factory reset (supprime config + reboot)
    """
    _gpio_export(BUTTON_LINE)
    _gpio_set_direction(BUTTON_LINE, "in")
    _gpio_enable_wakeup(BUTTON_LINE)

    logger.info(
        "Surveillance bouton GPIO %d. 5s → suspend, 20s → factory reset.",
        BUTTON_LINE,
    )

    try:
        while True:
            if _gpio_get_value(BUTTON_LINE) == 1:
                time.sleep(POLL_INTERVAL)
                continue

            if not _debounced_read(BUTTON_LINE, 0):
                continue

            press_time = time.monotonic()
            logger.info("Bouton pressé, décompte...")

            while True:
                if _gpio_get_value(BUTTON_LINE) == 1:
                    if not _debounced_read(BUTTON_LINE, 1):
                        continue
                    held = time.monotonic() - press_time
                    if held < HOLD_DURATION_SUSPEND:
                        logger.info("Bouton relâché après %.1fs — ignoré.", held)
                    break

                held = time.monotonic() - press_time
                if held >= HOLD_DURATION_RESET:
                    trigger_factory_reset()
                    return

                if held >= HOLD_DURATION_SUSPEND:
                    trigger_suspend()
                    return

                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Arrêt du daemon bouton.")
    finally:
        _gpio_unexport(BUTTON_LINE)
        logger.info("GPIO libéré.")


def main() -> None:
    _load_gpio_conf()
    logger.info("GygesLink button daemon démarrage...")
    watch_button()


if __name__ == "__main__":
    main()