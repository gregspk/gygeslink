#!/usr/bin/env python3
"""
GygesLink — Daemon LED RGB (Orange Pi Zero 2W)

Indique l'état de protection du boîtier en temps réel via une LED RGB.

Câblage GPIO (Orange Pi Zero 2W — Allwinner H618, header 26-pin) :
  Vérification obligatoire avant déploiement :
    gpiodetect                          # lister les chips
    gpioinfo gpiochip0 | grep -i "PH"   # vérifier les lignes PH

  GPIO_R → Pin 11 (PH9)  + résistance 100Ω → anode rouge   (Vf≈2.0V, If≈13mA)
  GPIO_G → Pin 13 (PH11) + résistance 100Ω → anode verte   (Vf≈2.2V, If≈11mA)
  GPIO_B → Pin 15 (PH12) + résistance 27Ω  → anode bleue   (Vf≈3.0V, If≈11mA)
  GND    → cathode commune (Pin 6, 9, 14, 20, 25)
  NOTE : H618 GPIO = 3.3V. Les résistances 330Ω sont pour 5V (inadaptées ici).

  Si les numéros de ligne gpiod diffèrent des valeurs par défaut,
  créer /data/gygeslink/gpio.conf avec les bonnes valeurs (voir ci-dessous).

États :
  Bleu clignotant rapide  (0.3s ON / 0.3s OFF)  → Mode setup (premier boot)
  Rouge clignotant rapide (0.5s ON / 0.5s OFF)  → Erreur critique, trafic BLOQUÉ
  Orange fixe                                   → Tor OK, protection partielle
  Orange clignotant lent  (1s ON / 2s OFF)      → Voucher Mullvad expire < 7j
  Vert fixe                                     → Protection complète

Logique de décision (par priorité) :
  1. setup-done absent                  → bleu clignotant
  2. Tor inactif OU iptables-open inactif → rouge clignotant
  3. couche manquante (noise/jitter/wg) → orange fixe
  4. voucher Mullvad expire bientôt     → orange clignotant lent
  5. tout OK                            → vert fixe
"""

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
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
#   Pin 11 = PH9  = ligne 224+9  = 233  → LED Rouge
#   Pin 13 = PH11 = ligne 224+11 = 235  → LED Vert
#   Pin 15 = PH12 = ligne 224+12 = 236  → LED Bleu
#   Pin  7 = PH14 = ligne 224+14 = 238  → Bouton
#
# VALEURS PAR DÉFAUT — à confirmer avec `gpioinfo gpiochip0` sur le Pi.
# Si différentes, créer /data/gygeslink/gpio.conf :
#   GPIOCHIP=gpiochip0
#   GPIO_R=233
#   GPIO_G=235
#   GPIO_B=236
#   BUTTON_LINE=238
GPIOCHIP = "gpiochip0"
GPIO_R   = 233
GPIO_G   = 235
GPIO_B   = 236

GPIO_CONF_FILE = Path("/data/gygeslink/gpio.conf")


def _load_gpio_conf() -> None:
    global GPIOCHIP, GPIO_R, GPIO_G, GPIO_B
    if not GPIO_CONF_FILE.exists():
        return
    mapping = {"GPIOCHIP": GPIOCHIP, "GPIO_R": GPIO_R, "GPIO_G": GPIO_G, "GPIO_B": GPIO_B}
    for line in GPIO_CONF_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in mapping:
            mapping[key] = int(val.strip()) if key != "GPIOCHIP" else val.strip()
    GPIOCHIP = mapping["GPIOCHIP"]
    GPIO_R = mapping["GPIO_R"]
    GPIO_G = mapping["GPIO_G"]
    GPIO_B = mapping["GPIO_B"]


SETUP_DONE_FILE  = Path("/data/gygeslink/setup-done")
WG_CONF_FILE     = Path("/data/gygeslink/wg0.conf")
WG_EXPIRY_FILE   = Path("/data/gygeslink/wg-expiry.txt")

EXPIRY_WARNING_DAYS = 7
CHECK_INTERVAL = 5.0

# ─────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [led] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("led")


# ─────────────────────────────────────────────────────────────────────
# Contrôle GPIO via libgpiod
# ─────────────────────────────────────────────────────────────────────

_lines: dict = {}


def gpio_setup() -> None:
    """Initialise les lignes GPIO en sortie, LED éteinte."""
    chip = gpiod.Chip(GPIOCHIP)
    for name, num in [("r", GPIO_R), ("g", GPIO_G), ("b", GPIO_B)]:
        line = chip.get_line(num)
        line.request(
            consumer="gygeslink-led",
            type=gpiod.LINE_REQ_DIR_OUT,
            default_vals=[0],
        )
        _lines[name] = line


def set_color(r: bool, g: bool, b: bool) -> None:
    """Applique une couleur RGB à la LED."""
    if not GPIO_AVAILABLE or not _lines:
        return
    _lines["r"].set_value(1 if r else 0)
    _lines["g"].set_value(1 if g else 0)
    _lines["b"].set_value(1 if b else 0)


def led_off() -> None:
    """Éteint la LED."""
    set_color(False, False, False)


def gpio_cleanup() -> None:
    """Libère les lignes GPIO."""
    for line in _lines.values():
        try:
            line.release()
        except Exception:
            pass
    _lines.clear()


# ─────────────────────────────────────────────────────────────────────
# Détection de l'état du système
# ─────────────────────────────────────────────────────────────────────

def service_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
    )
    return result.returncode == 0


def voucher_expiring_soon() -> bool:
    if not WG_EXPIRY_FILE.exists():
        return False
    try:
        expiry_str = WG_EXPIRY_FILE.read_text().strip()
        expiry = datetime.fromisoformat(expiry_str)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        delta = expiry - datetime.now(timezone.utc)
        return 0 < delta.days < EXPIRY_WARNING_DAYS
    except (ValueError, OSError) as e:
        logger.debug("Impossible de lire wg-expiry.txt : %s", e)
        return False


def get_system_state() -> str:
    if not SETUP_DONE_FILE.exists():
        return "setup"

    tor_ok = service_active("gygeslink-tor")
    iptables_ok = service_active("gygeslink-iptables-open")

    if not tor_ok or not iptables_ok:
        return "error"

    noise_ok  = service_active("gygeslink-noise")
    jitter_ok = service_active("gygeslink-jitter")

    wg_expected = WG_CONF_FILE.exists()
    wg_ok = service_active("gygeslink-wireguard") if wg_expected else True

    if not noise_ok or not jitter_ok or not wg_ok:
        return "partial"

    if wg_expected and voucher_expiring_soon():
        return "expiring"

    return "ok"


# ─────────────────────────────────────────────────────────────────────
# Patterns de clignotement
# ─────────────────────────────────────────────────────────────────────

def blink_setup() -> None:
    set_color(False, False, True)
    time.sleep(0.3)
    led_off()
    time.sleep(0.3)


def blink_error() -> None:
    set_color(True, False, False)
    time.sleep(0.5)
    led_off()
    time.sleep(0.5)


def show_partial() -> None:
    set_color(True, True, False)  # Rouge + Vert = Orange
    time.sleep(2.0)


def blink_expiring() -> None:
    set_color(True, True, False)
    time.sleep(1.0)
    led_off()
    time.sleep(2.0)


def show_ok() -> None:
    set_color(False, True, False)
    time.sleep(2.0)


# ─────────────────────────────────────────────────────────────────────
# Boucle principale
# ─────────────────────────────────────────────────────────────────────

def led_loop() -> None:
    current_state = "error"
    last_check = 0.0

    logger.info(
        "LED daemon démarré. Chip: %s R=%d G=%d B=%d",
        GPIOCHIP, GPIO_R, GPIO_G, GPIO_B,
    )

    while True:
        now = time.monotonic()
        if now - last_check >= CHECK_INTERVAL:
            new_state = get_system_state()
            if new_state != current_state:
                logger.info("État : %s → %s", current_state, new_state)
            current_state = new_state
            last_check = now

        if current_state == "setup":
            blink_setup()
        elif current_state == "error":
            blink_error()
        elif current_state == "partial":
            show_partial()
        elif current_state == "expiring":
            blink_expiring()
        else:
            show_ok()


# ─────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_gpio_conf()

    if not GPIO_AVAILABLE:
        logger.warning("gpiod non disponible — mode simulation (pas de LED physique).")
        logger.warning("Installer : apt install python3-libgpiod")

    if GPIO_AVAILABLE:
        gpio_setup()

    try:
        led_loop()
    except KeyboardInterrupt:
        logger.info("Arrêt du daemon LED.")
    finally:
        if GPIO_AVAILABLE:
            led_off()
            gpio_cleanup()
            logger.info("GPIO libéré.")


if __name__ == "__main__":
    main()
