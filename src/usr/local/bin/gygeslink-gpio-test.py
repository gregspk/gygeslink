#!/usr/bin/env python3
"""
GygesLink — Utilitaire de test GPIO (Orange Pi Zero 2W)

Teste chaque ligne GPIO configurée dans /data/gygeslink/gpio.conf :
  1. Allume chaque couleur de LED (R, G, B) pendant 2s
  2. Lit l'état du bouton en continu pendant 10s
  3. Affiche les correspondances Pin ↔ gpiod line

Usage :
  sudo python3 /usr/local/bin/gygeslink-gpio-test.py
"""

import sys
import time
from pathlib import Path

try:
    import gpiod
except ImportError:
    print("ERREUR : python3-libgpiod non installé.")
    print("Installer : sudo apt install python3-libgpiod gpiod")
    sys.exit(1)

GPIO_CONF_FILE = Path("/data/gygeslink/gpio.conf")

DEFAULTS = {
    "GPIOCHIP": "gpiochip0",
    "GPIO_R": 233,
    "GPIO_G": 235,
    "GPIO_B": 236,
    "BUTTON_LINE": 238,
}

PIN_MAP = {
    "GPIO_R": "Pin 11 (PH9) — LED Rouge",
    "GPIO_G": "Pin 13 (PH11) — LED Verte",
    "GPIO_B": "Pin 15 (PH12) — LED Bleue",
    "BUTTON_LINE": "Pin 7 (PH14) — Bouton",
}


def load_conf():
    conf = dict(DEFAULTS)
    if GPIO_CONF_FILE.exists():
        for line in GPIO_CONF_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in conf:
                conf[key] = int(val.strip()) if key != "GPIOCHIP" else val.strip()
    else:
        print(f"WARN : {GPIO_CONF_FILE} non trouvé — utilisation des valeurs par défaut")
    return conf


def discover_gpio():
    print("\n=== GPIO Discovery ===")
    for i in range(5):
        chip_name = f"gpiochip{i}"
        try:
            chip = gpiod.Chip(chip_name)
            print(f"\n{chip_name} : {chip.name()} ({chip.num_lines()} lignes)")
            ph_lines = []
            for line_num in range(min(chip.num_lines(), 300)):
                try:
                    line = chip.get_line(line_num)
                    info = line.to_info()
                    name = info.name if hasattr(info, 'name') else ""
                    consumer = info.consumer if hasattr(info, 'consumer') else ""
                    if "PH" in name.upper() or line_num >= 224:
                        ph_lines.append((line_num, name, consumer))
                except Exception:
                    pass
            if ph_lines:
                print(f"  Lignes PH trouvées :")
                for num, name, consumer in ph_lines[:30]:
                    print(f"    ligne {num}: {name} [{consumer}]")
        except OSError:
            break


def test_led(chip_name, gpio_r, gpio_g, gpio_b):
    print("\n=== Test LED RGB ===")
    print(f"gpiochip: {chip_name}, R={gpio_r}, G={gpio_g}, B={gpio_b}")
    chip = gpiod.Chip(chip_name)

    colors = [
        ("Rouge", gpio_r, True, False, False),
        ("Vert", gpio_g, False, True, False),
        ("Bleu", gpio_b, False, False, True),
        ("Blanc (R+G+B)", None, True, True, True),
    ]

    lines = {}
    for name, num in [("r", gpio_r), ("g", gpio_g), ("b", gpio_b)]:
        line = chip.get_line(num)
        line.request(consumer="gpio-test", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0])
        lines[name] = line

    for color_name, _, r, g, b in colors:
        print(f"  Allumage {color_name} (2s)... ", end="", flush=True)
        lines["r"].set_value(1 if r else 0)
        lines["g"].set_value(1 if g else 0)
        lines["b"].set_value(1 if b else 0)
        time.sleep(2)
        lines["r"].set_value(0)
        lines["g"].set_value(0)
        lines["b"].set_value(0)
        print("OK")

    for line in lines.values():
        line.release()
    print("LED : test terminé.")


def test_button(chip_name, button_line, duration=10):
    print(f"\n=== Test Bouton (Pin 7, ligne {button_line}) ===")
    print(f"Appuie sur le bouton dans les {duration}s suivantes...")
    chip = gpiod.Chip(chip_name)
    line = chip.get_line(button_line)
    line.request(
        consumer="gpio-test",
        type=gpiod.LINE_REQ_DIR_IN,
        flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
    )

    start = time.monotonic()
    press_count = 0
    last_state = 1

    while time.monotonic() - start < duration:
        val = line.get_value()
        if val != last_state:
            state_str = "PRESSÉ" if val == 0 else "RELÂCHÉ"
            print(f"  [{time.monotonic()-start:.1f}s] Bouton {state_str}")
            last_state = val
            if val == 0:
                press_count += 1
        time.sleep(0.02)

    if press_count == 0:
        print("  Aucun appui détecté — vérifier le câblage.")
    else:
        print(f"  {press_count} appui(s) détecté(s) — bouton OK!")

    line.release()


def main():
    print("GygesLink — Test GPIO Orange Pi Zero 2W\n")
    conf = load_conf()

    for key in ["GPIOCHIP", "GPIO_R", "GPIO_G", "GPIO_B", "BUTTON_LINE"]:
        pin_info = PIN_MAP.get(key, "")
        print(f"  {key:15s} = {conf[key]}  ({pin_info})")

    discover_gpio()

    try:
        test_led(conf["GPIOCHIP"], conf["GPIO_R"], conf["GPIO_G"], conf["GPIO_B"])
    except Exception as e:
        print(f"\nERREUR LED : {e}")
        print("Vérifiez gpio.conf et les numéros de ligne gpiod.")

    try:
        test_button(conf["GPIOCHIP"], conf["BUTTON_LINE"])
    except Exception as e:
        print(f"\nERREUR Bouton : {e}")
        print("Vérifiez gpio.conf et les numéros de ligne gpiod.")

    print("\nPour ajuster les numéros GPIO :")
    print(f"  sudo nano {GPIO_CONF_FILE}")


if __name__ == "__main__":
    main()