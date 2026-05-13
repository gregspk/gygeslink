#!/usr/bin/env python3
"""
GygesLink — Daemon LED RGB (Orange Pi Zero 2W)

Indique l'état de protection du boîtier en temps réel via une LED RGB.

Câblage GPIO (Orange Pi Zero 2W — Allwinner H618, header 26-pin) :
  Vérification obligatoire avant déploiement :
    gpiodetect                          # lister les chips
    gpioinfo gpiochip0 | grep -i "PH"   # vérifier les lignes PH

  LED RGB ANODE COMMUNE (la longue broche va au 3.3V, pas au GND) :
    Broche longue (anode commune) → Pin 1 (3.3V)
    Broche 1 (rouge, cathode)  → R 82Ω  → Pin 11 (PH9)
    Broche 3 (vert, cathode)    → R 22Ω  → Pin 13 (PH11)
    Broche 4 (bleu, cathode)    → R 22Ω  → Pin 15 (PH12)
  LOGIQUE INVERSÉE : GPIO LOW = LED ON, GPIO HIGH = LED OFF
  H618 GPIO = 3.3V. LED diffusée (Vf G/B ≈ 3.0V) compatible 3.3V.

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
# LED RGB ANODE COMMUNE : GPIO LOW = LED ON, GPIO HIGH = LED OFF
# VALEURS PAR DÉFAUT — à confirmer avec `gpioinfo gpiochip0` sur le Pi.
# Si différentes, créer /data/gygeslink/gpio.conf :
#   GPIOCHIP=gpiochip0
#   GPIO_R=233
#   GPIO_G=235
#   GPIO_B=236
#   BUTTON_LINE=238
GPIOCHIP = "/dev/gpiochip1"
GPIO_R   = 226
GPIO_G   = 227
GPIO_B   = 261

LED_ACTIVE_LOW = True

GPIO_CONF_FILE = Path("/data/gygeslink/gpio.conf")


def _load_gpio_conf() -> None:
    global GPIOCHIP, GPIO_R, GPIO_G, GPIO_B, LED_ACTIVE_LOW
    if not GPIO_CONF_FILE.exists():
        return
    mapping = {"GPIOCHIP": GPIOCHIP, "GPIO_R": GPIO_R, "GPIO_G": GPIO_G, "GPIO_B": GPIO_B, "LED_ACTIVE_LOW": str(LED_ACTIVE_LOW)}
    for line in GPIO_CONF_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key in mapping:
            if key == "GPIOCHIP":
                mapping[key] = val
            elif key == "LED_ACTIVE_LOW":
                mapping[key] = val.lower() in ("true", "1", "yes")
            else:
                mapping[key] = int(val)
    GPIOCHIP = mapping["GPIOCHIP"]
    GPIO_R = mapping["GPIO_R"]
    GPIO_G = mapping["GPIO_G"]
    GPIO_B = mapping["GPIO_B"]
    LED_ACTIVE_LOW = mapping["LED_ACTIVE_LOW"]


SETUP_DONE_FILE  = Path("/data/gygeslink/setup-done")
WG_CONF_FILE     = Path("/data/gygeslink/wg0.conf")
WG_EXPIRY_FILE   = Path("/data/gygeslink/wg-expiry.txt")
PAUSED_FILE      = Path("/data/gygeslink/paused")

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
# Contrôle GPIO via libgpiod v2
# ─────────────────────────────────────────────────────────────────────

_request = None


def gpio_setup() -> None:
    """Initialise les lignes GPIO en sortie, LED éteinte."""
    global _request
    initial = [1, 1, 1] if LED_ACTIVE_LOW else [0, 0, 0]
    _request = gpiod.request_lines(
        GPIOCHIP,
        consumer="gygeslink-led",
        offsets=[GPIO_R, GPIO_G, GPIO_B],
        direction=gpiod.line.Direction.OUTPUT,
        output_values=initial,
    )


def set_color(r: bool, g: bool, b: bool) -> None:
    """Applique une couleur RGB à la LED.
    En mode anode commune (LED_ACTIVE_LOW=True) : LOW = ON, HIGH = OFF.
    """
    if not GPIO_AVAILABLE or _request is None:
        return
    on, off = (0, 1) if LED_ACTIVE_LOW else (1, 0)
    _request.set_values([on if r else off, on if g else off, on if b else off])


def led_off() -> None:
    """Éteint la LED."""
    set_color(False, False, False)


def gpio_cleanup() -> None:
    """Libère les lignes GPIO."""
    global _request
    if _request is not None:
        try:
            _request.release()
        except Exception:
            pass
        _request = None


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
    if PAUSED_FILE.exists():
        return "paused"
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


def show_paused() -> None:
    set_color(False, False, True)
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
        elif current_state == "paused":
            show_paused()
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
