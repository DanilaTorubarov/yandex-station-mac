#!/bin/sh
# $1 - AirPlay volume in dB (e.g. "-12.00" or "0.00" or "-144.00" for mute)
echo "$(date): db=$1" >> /tmp/airplay-vol.log 2>/dev/null
db="$1"
[ -z "$db" ] && exit 0

int_db=$(echo "$db" | cut -d. -f1 | tr -d '-')

if [ "$db" = "-144.00" ] || [ "$int_db" -ge 80 ]; then
    lvl=0
elif [ "$int_db" -le 1 ]; then
    lvl=10
elif [ "$int_db" -le 4 ]; then
    lvl=9
elif [ "$int_db" -le 7 ]; then
    lvl=8
elif [ "$int_db" -le 10 ]; then
    lvl=7
elif [ "$int_db" -le 13 ]; then
    lvl=6
elif [ "$int_db" -le 16 ]; then
    lvl=5
elif [ "$int_db" -le 19 ]; then
    lvl=4
elif [ "$int_db" -le 22 ]; then
    lvl=3
elif [ "$int_db" -le 25 ]; then
    lvl=2
else
    lvl=1
fi

master_val=$(( lvl * 25 ))
[ "$master_val" -gt 255 ] && master_val=255

case "$lvl" in
    0)  spk_val=0 ;;
    1)  spk_val=175 ;;
    2)  spk_val=185 ;;
    3)  spk_val=195 ;;
    4)  spk_val=203 ;;
    5)  spk_val=211 ;;
    6)  spk_val=217 ;;
    7)  spk_val=223 ;;
    8)  spk_val=229 ;;
    9)  spk_val=234 ;;
    10) spk_val=239 ;;
    *)  spk_val=211 ;;
esac

if [ "$lvl" -eq 0 ]; then
    amixer sset "Master" 0 mute >/dev/null 2>&1
    amixer sset "SPK Master" 0 mute >/dev/null 2>&1
else
    amixer sset "Master" "$master_val" unmute >/dev/null 2>&1
    amixer sset "SPK Master" "$spk_val" unmute >/dev/null 2>&1
fi
