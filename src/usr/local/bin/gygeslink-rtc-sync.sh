#!/bin/bash
# GygesLink - Synchronisation horloge DS3231 RTC
# Lit l'heure du RTC via sysfs et l'applique à l'horloge système.
# hwclock n'existe plus dans util-linux 2.41+ (Armbian Bookworm).

set -euo pipefail

RTC_TIME="/sys/class/rtc/rtc0/time"
RTC_DATE="/sys/class/rtc/rtc0/date"

if [ ! -f "$RTC_TIME" ] || [ ! -f "$RTC_DATE" ]; then
    echo "[rtc-sync] RTC non détecté via sysfs — heure système non synchronisée."
    exit 0
fi

RTC_TIME_VAL=$(cat "$RTC_TIME" | tr -d '\n')
RTC_DATE_VAL=$(cat "$RTC_DATE" | tr -d '\n')

if [ -z "$RTC_TIME_VAL" ] || [ -z "$RTC_DATE_VAL" ]; then
    echo "[rtc-sync] RTC présent mais valeur vide — heure système non synchronisée."
    exit 0
fi

# Format attendu : date="2026-05-13" time="14:30:00"
# date -s attend : "2026-05-13 14:30:00"
DATETIME="${RTC_DATE_VAL} ${RTC_TIME_VAL}"

date -s "$DATETIME" 2>/dev/null || {
    echo "[rtc-sync] Impossible d'appliquer l'heure RTC : $DATETIME"
    exit 1
}

echo "[rtc-sync] Heure RTC appliquée : $DATETIME"