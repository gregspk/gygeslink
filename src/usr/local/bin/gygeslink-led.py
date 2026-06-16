#!/usr/bin/env python3
"""
GygesLink — Daemon LED RGB (Orange Pi Zero 2W)

Indique l'état de protection du boîtier en temps réel via une LED RGB.

Câblage GPIO (Orange Pi Zero 2W — Allwinner H618, header 26-pin) :
  LED RGB ANODE COMMUNE (la longue broche va au 3.3V, pas au GND) :
    Pin 1  (3.3V)  → Anode commune LED
    Pin 8  (PH2)   → R 82Ω → Cathode rouge   (GPIO 226)
    Pin 10 (PH3)   → R 22Ω → Cathode vert    (GPIO 227)
    Pin 15 (PI5)   → R 22Ω → Cathode bleu    (GPIO 261)
  LOGIQUE INVERSÉE : GPIO LOW = LED ON, GPIO HIGH = LED OFF

  Si les numéros GPIO diffèrent, créer /data/gygeslink/gpio.conf :
    GPIO_R=226
    GPIO_G=227
    GPIO_B=261

Utilise sysfs (/sys/class/gpio) pour le contrôle GPIO — compatible
avec tous les kernels, sans dépendance à une version de libgpiod.

États :
  Blanc clignotant rapide (0.3s ON / 0.3s OFF) → Mode setup (premier boot) ou démarrage
  Bleu fixe                              → Fonctionnement normal, protection complète
  Orange fixe                            → Mode pause (trafic non anonymisé)
  Rouge clignotant rapide (0.5s ON / 0.5s OFF) → Erreur critique, trafic BLOQUÉ
"""

import logging
import os
import sys
import time
from pathlib import Path

SETUP_DONE_FILE  = Path("/data/gygeslink/setup-done")
PAUSED_FILE      = Path("/data/gygeslink/paused")

GPIO_R = 226
GPIO_G = 227
GPIO_B = 261

LED_ACTIVE_LOW = True

GPIO_CONF_FILE = Path("/data/gygeslink/gpio.conf")

CHECK_INTERVAL = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [led] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("led")


def _load_gpio_conf() -> None:
    global GPIO_R, GPIO_G, GPIO_B, LED_ACTIVE_LOW
    if not GPIO_CONF_FILE.exists():
        return
    mapping = {"GPIO_R": GPIO_R, "GPIO_G": GPIO_G, "GPIO_B": GPIO_B, "LED_ACTIVE_LOW": str(LED_ACTIVE_LOW)}
    for line in GPIO_CONF_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key in mapping:
            if key == "LED_ACTIVE_LOW":
                mapping[key] = val.lower() in ("true", "1", "yes")
            else:
                mapping[key] = int(val)
    GPIO_R = mapping["GPIO_R"]
    GPIO_G = mapping["GPIO_G"]
    GPIO_B = mapping["GPIO_B"]
    LED_ACTIVE_LOW = mapping["LED_ACTIVE_LOW"]


_gpio_exported = set()


def _gpio_write(path: str, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


def _cleanup_stale_gpio() -> None:
    for entry in Path("/sys/class/gpio").iterdir():
        name = entry.name
        if name.startswith("gpio") and name[4:].isdigit():
            pin = int(name[4:])
            if pin not in (GPIO_R, GPIO_G, GPIO_B):
                try:
                    _gpio_write("/sys/class/gpio/unexport", str(pin))
                except OSError:
                    pass


def gpio_export(pin: int) -> None:
    gpio_path = Path(f"/sys/class/gpio/gpio{pin}")
    if gpio_path.exists():
        _gpio_exported.add(pin)
        return
    for attempt in range(5):
        try:
            _gpio_write("/sys/class/gpio/export", str(pin))
            _gpio_exported.add(pin)
            return
        except OSError:
            time.sleep(0.1)
    if gpio_path.exists():
        _gpio_exported.add(pin)
        return
    raise RuntimeError(f"Cannot export GPIO {pin} after 5 attempts")


def gpio_set_direction(pin: int, direction: str) -> None:
    _gpio_write(f"/sys/class/gpio/gpio{pin}/direction", direction)


def gpio_set_value(pin: int, value: int) -> None:
    _gpio_write(f"/sys/class/gpio/gpio{pin}/value", str(value))


def gpio_unexport(pin: int) -> None:
    gpio_path = Path(f"/sys/class/gpio/gpio{pin}")
    if gpio_path.exists():
        try:
            _gpio_write("/sys/class/gpio/unexport", str(pin))
        except OSError:
            pass
    _gpio_exported.discard(pin)


def gpio_setup() -> None:
    for pin in (GPIO_R, GPIO_G, GPIO_B):
        gpio_export(pin)
        gpio_set_direction(pin, "out")
        initial = 1 if LED_ACTIVE_LOW else 0
        gpio_set_value(pin, initial)


def set_color(r: bool, g: bool, b: bool) -> None:
    on, off = (0, 1) if LED_ACTIVE_LOW else (1, 0)
    gpio_set_value(GPIO_R, on if r else off)
    gpio_set_value(GPIO_G, on if g else off)
    gpio_set_value(GPIO_B, on if b else off)


def led_off() -> None:
    set_color(False, False, False)


def gpio_cleanup() -> None:
    for pin in (GPIO_R, GPIO_G, GPIO_B):
        if pin in _gpio_exported:
            try:
                gpio_unexport(pin)
            except Exception:
                pass


def service_active(name: str) -> bool:
    import subprocess
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
    )
    return result.returncode == 0


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

    if not noise_ok or not jitter_ok:
        return "partial"

    return "ok"


def blink_setup() -> None:
    set_color(True, True, True)
    time.sleep(0.3)
    led_off()
    time.sleep(0.3)


def blink_error() -> None:
    set_color(True, False, False)
    time.sleep(0.5)
    led_off()
    time.sleep(0.5)


def show_partial() -> None:
    set_color(True, True, False)
    time.sleep(2.0)


def show_ok() -> None:
    set_color(False, False, True)
    time.sleep(2.0)


def show_paused() -> None:
    set_color(True, True, False)
    time.sleep(2.0)


def led_loop() -> None:
    current_state = "error"
    last_check = 0.0

    logger.info(
        "LED daemon démarré. R=%d G=%d B=%d",
        GPIO_R, GPIO_G, GPIO_B,
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
        else:
            show_ok()


def main() -> None:
    _load_gpio_conf()

    gpio_setup()

    try:
        led_loop()
    except KeyboardInterrupt:
        logger.info("Arrêt du daemon LED.")
    finally:
        led_off()
        gpio_cleanup()
        logger.info("GPIO libéré.")


if __name__ == "__main__":
    main()