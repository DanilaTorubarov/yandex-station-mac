#!/bin/sh
PID_FILE="/tmp/timer_clock.pid"

# Segments for digits 0-9
SEG_0="A B C D E F"
SEG_1="B C"
SEG_2="A B D E G"
SEG_3="A B C D G"
SEG_4="B C F G"
SEG_5="A C D F G"
SEG_6="A C D E F G"
SEG_7="A B C"
SEG_8="A B C D E F G"
SEG_9="A B C D F G"

render_all() {
    b="$1"
    d1="$2"
    d2="$3"
    d3="$4"
    d4="$5"

    for pos in 1 2 3 4; do
        eval "val=\$d$pos"
        eval "segs=\$SEG_${val}"
        for s in A B C D E F G; do
            f="/sys/class/leds/mini2:white:clock${s}${pos}/brightness"
            match=0
            for on_s in $segs; do
                if [ "$on_s" = "$s" ]; then
                    match=1
                    break
                fi
            done
            if [ "$match" -eq 1 ]; then
                echo "$b" > "$f"
            else
                echo 0 > "$f"
            fi
        done
    done
    echo "$b" > /sys/class/leds/mini2:white:clockPLUS1/brightness
    echo "$b" > /sys/class/leds/mini2:white:clockPLUS2/brightness
}

stop_timer() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ]; then
            kill -15 "$pid" 2>/dev/null
        fi
        rm -f "$PID_FILE"
    fi
    for p in $(ps -o pid,args 2>/dev/null | awk "/timer-clock\.sh loop/ && !/awk/ {print \$1}"); do
        kill -9 "$p" 2>/dev/null
    done
}

run_loop() {
    rem="$1"
    b_val="$2"

    while [ "$rem" -ge 0 ]; do
        b=$(cat /sys/class/leds/mini2:white:clockPLUS1/brightness 2>/dev/null)
        if [ -z "$b" ] || [ "$b" -le 0 ]; then
            b="$b_val"
        fi

        if [ "$rem" -ge 3600 ]; then
            hrs=$(( rem / 3600 ))
            mins=$(( (rem % 3600) / 60 ))
            d1=$(( hrs / 10 ))
            d2=$(( hrs % 10 ))
            d3=$(( mins / 10 ))
            d4=$(( mins % 10 ))
        else
            mins=$(( rem / 60 ))
            secs=$(( rem % 60 ))
            d1=$(( mins / 10 ))
            d2=$(( mins % 10 ))
            d3=$(( secs / 10 ))
            d4=$(( secs % 10 ))
        fi

        render_all "$b" "$d1" "$d2" "$d3" "$d4"

        if [ "$rem" -eq 0 ]; then
            break
        fi
        rem=$(( rem - 1 ))
        sleep 1
    done

    # Flash 00:00 when timer is up
    for i in 1 2 3; do
        render_all 0 0 0 0 0
        usleep 250000 2>/dev/null || sleep 1
        render_all "$b" 0 0 0 0
        usleep 350000 2>/dev/null || sleep 1
    done

    rm -f "$PID_FILE"
}

case "$1" in
    stop)
        stop_timer
        ;;
    start)
        stop_timer
        dur="${2:-60}"
        b_init=$(cat /sys/class/leds/mini2:white:clockPLUS1/brightness 2>/dev/null)
        if [ -z "$b_init" ] || [ "$b_init" -le 0 ]; then
            b_init=22
        fi
        (
            echo "$$" > "$PID_FILE"
            exec "$0" loop "$dur" "$b_init"
        ) >/dev/null 2>&1 &
        ;;
    loop)
        run_loop "$2" "$3"
        ;;
esac
