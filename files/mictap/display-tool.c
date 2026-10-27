/*
 * display-tool — Управление 4-разрядным 7-сегментным LED-дисплеем и индикаторами
 * Яндекс Станции Мини 2.
 *
 * Аппаратная конфигурация:
 * - 4 цифры по 7 сегментов (A..G) = 28 белых светодиодов
 * - clockPLUS1, clockPLUS2 — двоеточие между 2 и 3 цифрами (часы HH:MM)
 * - clockPLUS3, clockPLUS4 — дополнительные индикаторы
 * - clockMINUS — знак минус для отрицательной температуры
 * - clockDEGREE — значок градуса (°)
 */

#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define PID_FILE "/tmp/display_tool.pid"
#define BRIGHTNESS_FILE "/tmp/display_brightness"

static volatile int keep_running = 1;

static void handle_sig(int sig) {
    (void)sig;
    keep_running = 0;
}

enum {
    LED_A1 = 0, LED_B1, LED_C1, LED_D1, LED_E1, LED_F1, LED_G1,
    LED_A2,     LED_B2, LED_C2, LED_D2, LED_E2, LED_F2, LED_G2,
    LED_A3,     LED_B3, LED_C3, LED_D3, LED_E3, LED_F3, LED_G3,
    LED_A4,     LED_B4, LED_C4, LED_D4, LED_E4, LED_F4, LED_G4,
    LED_PLUS1,
    LED_PLUS2,
    LED_PLUS3,
    LED_PLUS4,
    LED_MINUS,
    LED_DEGREE,
    NUM_LEDS
};

static const char *LED_NAMES[NUM_LEDS] = {
    "mini2:white:clockA1", "mini2:white:clockB1", "mini2:white:clockC1",
    "mini2:white:clockD1", "mini2:white:clockE1", "mini2:white:clockF1", "mini2:white:clockG1",
    "mini2:white:clockA2", "mini2:white:clockB2", "mini2:white:clockC2",
    "mini2:white:clockD2", "mini2:white:clockE2", "mini2:white:clockF2", "mini2:white:clockG2",
    "mini2:white:clockA3", "mini2:white:clockB3", "mini2:white:clockC3",
    "mini2:white:clockD3", "mini2:white:clockE3", "mini2:white:clockF3", "mini2:white:clockG3",
    "mini2:white:clockA4", "mini2:white:clockB4", "mini2:white:clockC4",
    "mini2:white:clockD4", "mini2:white:clockE4", "mini2:white:clockF4", "mini2:white:clockG4",
    "mini2:white:clockPLUS1",
    "mini2:white:clockPLUS2",
    "mini2:white:clockPLUS3",
    "mini2:white:clockPLUS4",
    "mini2:white:clockMINUS",
    "mini2:white:clockDEGREE"
};

static int led_fds[NUM_LEDS];
static int default_brightness = 25;

static void cmd_clock(int seconds);

static void init_leds(void) {
    if (!getenv("TZ")) {
        setenv("TZ", "MSK-3", 1);
        tzset();
    }

    char path[128];
    for (int i = 0; i < NUM_LEDS; ++i) {
        snprintf(path, sizeof(path), "/sys/class/leds/%s/brightness", LED_NAMES[i]);
        led_fds[i] = open(path, O_WRONLY | O_CLOEXEC);
    }

    FILE *bf = fopen(BRIGHTNESS_FILE, "r");
    if (bf) {
        int b = 0;
        if (fscanf(bf, "%d", &b) == 1 && b > 0 && b <= 255) {
            default_brightness = b;
        }
        fclose(bf);
    }
}

static void close_leds(void) {
    for (int i = 0; i < NUM_LEDS; ++i) {
        if (led_fds[i] >= 0) {
            close(led_fds[i]);
            led_fds[i] = -1;
        }
    }
}

static void write_led(int idx, int val) {
    if (idx < 0 || idx >= NUM_LEDS || led_fds[idx] < 0) return;
    char buf[16];
    int n = snprintf(buf, sizeof(buf), "%d\n", val & 0xff);
    if (pwrite(led_fds[idx], buf, (size_t)n, 0) < 0) {}
}

/*
 * Битовая маска сегментов 7-сегментного индикатора:
 * bit 0: A (верхний)
 * bit 1: B (верхний правый)
 * bit 2: C (нижний правый)
 * bit 3: D (нижний)
 * bit 4: E (нижний левый)
 * bit 5: F (верхний левый)
 * bit 6: G (средний)
 */
#define SEG_A 0x01
#define SEG_B 0x02
#define SEG_C 0x04
#define SEG_D 0x08
#define SEG_E 0x10
#define SEG_F 0x20
#define SEG_G 0x40

static void render_raw(uint8_t d1, uint8_t d2, uint8_t d3, uint8_t d4,
                       int colon, int minus, int degree, int brightness) {
    uint8_t digits[4] = {d1, d2, d3, d4};
    int b = (brightness > 0) ? brightness : default_brightness;

    for (int pos = 0; pos < 4; ++pos) {
        uint8_t mask = digits[pos];
        int base = pos * 7;
        for (int s = 0; s < 7; ++s) {
            int on = (mask >> s) & 1;
            write_led(base + s, on ? b : 0);
        }
    }

    write_led(LED_PLUS1, colon ? b : 0);
    write_led(LED_PLUS2, colon ? b : 0);
    write_led(LED_MINUS, minus ? b : 0);
    write_led(LED_DEGREE, degree ? b : 0);
}

static void clear_display(void) {
    render_raw(0, 0, 0, 0, 0, 0, 0, 0);
    write_led(LED_PLUS3, 0);
    write_led(LED_PLUS4, 0);
}

static void stop_other_instances(void) {
    FILE *fp = fopen(PID_FILE, "r");
    if (fp) {
        pid_t pid = 0;
        if (fscanf(fp, "%d", &pid) == 1 && pid > 1 && pid != getpid()) {
            kill(pid, SIGTERM);
            usleep(50000);
            kill(pid, SIGKILL);
        }
        fclose(fp);
        unlink(PID_FILE);
    }
}

static void record_pid(void) {
    FILE *fp = fopen(PID_FILE, "w");
    if (fp) {
        fprintf(fp, "%d\n", getpid());
        fclose(fp);
    }
}

/* Конвертация символа (ASCII / Cyrillic) в 7-сегментную маску */
static uint8_t char_to_mask(uint32_t codepoint) {
    if (codepoint >= '0' && codepoint <= '9') {
        static const uint8_t nums[10] = {
            SEG_A|SEG_B|SEG_C|SEG_D|SEG_E|SEG_F,         /* 0 */
            SEG_B|SEG_C,                                 /* 1 */
            SEG_A|SEG_B|SEG_G|SEG_E|SEG_D,               /* 2 */
            SEG_A|SEG_B|SEG_G|SEG_C|SEG_D,               /* 3 */
            SEG_F|SEG_G|SEG_B|SEG_C,                     /* 4 */
            SEG_A|SEG_F|SEG_G|SEG_C|SEG_D,               /* 5 */
            SEG_A|SEG_F|SEG_E|SEG_D|SEG_C|SEG_G,         /* 6 */
            SEG_A|SEG_B|SEG_C,                           /* 7 */
            SEG_A|SEG_B|SEG_C|SEG_D|SEG_E|SEG_F|SEG_G,   /* 8 */
            SEG_A|SEG_B|SEG_C|SEG_D|SEG_F|SEG_G          /* 9 */
        };
        return nums[codepoint - '0'];
    }

    if (codepoint >= 'a' && codepoint <= 'z') codepoint -= 32;

    switch (codepoint) {
        case 'A': return SEG_A|SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;
        case 'B': return SEG_C|SEG_D|SEG_E|SEG_F|SEG_G;       /* b */
        case 'C': return SEG_A|SEG_D|SEG_E|SEG_F;
        case 'D': return SEG_B|SEG_C|SEG_D|SEG_E|SEG_G;       /* d */
        case 'E': return SEG_A|SEG_D|SEG_E|SEG_F|SEG_G;
        case 'F': return SEG_A|SEG_E|SEG_F|SEG_G;
        case 'G': return SEG_A|SEG_C|SEG_D|SEG_E|SEG_F;
        case 'H': return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;
        case 'I': return SEG_B|SEG_C;
        case 'J': return SEG_B|SEG_C|SEG_D|SEG_E;
        case 'K': return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;
        case 'L': return SEG_D|SEG_E|SEG_F;
        case 'M': return SEG_C|SEG_E|SEG_G;                   /* n */
        case 'N': return SEG_C|SEG_E|SEG_G;                   /* n */
        case 'O': return SEG_A|SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;
        case 'P': return SEG_A|SEG_B|SEG_E|SEG_F|SEG_G;
        case 'Q': return SEG_A|SEG_B|SEG_C|SEG_F|SEG_G;
        case 'R': return SEG_E|SEG_G;                         /* r */
        case 'S': return SEG_A|SEG_C|SEG_D|SEG_F|SEG_G;       /* 5 */
        case 'T': return SEG_D|SEG_E|SEG_F|SEG_G;             /* t */
        case 'U': return SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;
        case 'V': return SEG_C|SEG_D|SEG_E;                   /* u */
        case 'W': return SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;
        case 'X': return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;
        case 'Y': return SEG_B|SEG_C|SEG_D|SEG_F|SEG_G;
        case 'Z': return SEG_A|SEG_B|SEG_G|SEG_E|SEG_D;
        case '-': return SEG_G;
        case '_': return SEG_D;
        case '=': return SEG_D|SEG_G;
        case '^': return SEG_A|SEG_B|SEG_F;
        case '"': return SEG_B|SEG_F;
        case '\'': return SEG_B;
        case '`': return SEG_F;
        case '?': return SEG_A|SEG_B|SEG_E|SEG_G;
        case ' ': return 0;

        /* Русские буквы UTF-8 (Unicode) */
        case 0x0410: case 0x0430: return SEG_A|SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;       /* А */
        case 0x0411: case 0x0431: return SEG_A|SEG_C|SEG_D|SEG_E|SEG_F|SEG_G;       /* Б */
        case 0x0412: case 0x0432: return SEG_C|SEG_D|SEG_E|SEG_F|SEG_G;             /* В (b) */
        case 0x0413: case 0x0433: return SEG_A|SEG_E|SEG_F;                         /* Г */
        case 0x0414: case 0x0434: return SEG_B|SEG_C|SEG_D|SEG_E|SEG_G;             /* Д (d) */
        case 0x0415: case 0x0435: case 0x0401: case 0x0451:
                                  return SEG_A|SEG_D|SEG_E|SEG_F|SEG_G;             /* Е / Ё */
        case 0x0416: case 0x0436: return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;             /* Ж (H) */
        case 0x0417: case 0x0437: return SEG_A|SEG_B|SEG_G|SEG_C|SEG_D;             /* З (3) */
        case 0x0418: case 0x0438: case 0x0419: case 0x0439:
                                  return SEG_C|SEG_D|SEG_E;                         /* И / Й (u) */
        case 0x041A: case 0x043A: return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;             /* К */
        case 0x041B: case 0x043B: return SEG_D|SEG_E|SEG_F;                         /* Л (L) */
        case 0x041C: case 0x043C: return SEG_C|SEG_E|SEG_G;                         /* М (n) */
        case 0x041D: case 0x043D: return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;             /* Н (H) */
        case 0x041E: case 0x043E: return SEG_A|SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;       /* О (O) */
        case 0x041F: case 0x043F: return SEG_A|SEG_B|SEG_C|SEG_E|SEG_F;             /* П */
        case 0x0420: case 0x0440: return SEG_A|SEG_B|SEG_E|SEG_F|SEG_G;             /* Р (P) */
        case 0x0421: case 0x0441: return SEG_A|SEG_D|SEG_E|SEG_F;                   /* С (C) */
        case 0x0422: case 0x0442: return SEG_D|SEG_E|SEG_F|SEG_G;                   /* Т (t) */
        case 0x0423: case 0x0443: return SEG_B|SEG_C|SEG_D|SEG_F|SEG_G;             /* У (y) */
        case 0x0424: case 0x0444: return SEG_A|SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;       /* Ф */
        case 0x0425: case 0x0445: return SEG_B|SEG_C|SEG_E|SEG_F|SEG_G;             /* Х (H) */
        case 0x0426: case 0x0446: return SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;             /* Ц (U) */
        case 0x0427: case 0x0447: return SEG_F|SEG_G|SEG_B|SEG_C;                   /* Ч (4) */
        case 0x0428: case 0x0448: case 0x0429: case 0x0449:
                                  return SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;             /* Ш / Щ */
        case 0x042A: case 0x044A: case 0x042C: case 0x044C:
                                  return SEG_C|SEG_D|SEG_E|SEG_F|SEG_G;             /* Ъ / Ь (b) */
        case 0x042B: case 0x044B: return SEG_C|SEG_D|SEG_E|SEG_F|SEG_G;             /* Ы */
        case 0x042D: case 0x044D: return SEG_A|SEG_B|SEG_G|SEG_C|SEG_D;             /* Э (3) */
        case 0x042E: case 0x044E: return SEG_B|SEG_C|SEG_D|SEG_E|SEG_F;             /* Ю */
        case 0x042F: case 0x044F: return SEG_A|SEG_B|SEG_C|SEG_F|SEG_G;             /* Я (q) */
        default: return 0;
    }
}

/* Разбор строки UTF-8 в массив 7-сегментных масок */
static int parse_utf8_to_masks(const char *str, uint8_t *masks, int max_len) {
    int count = 0;
    const unsigned char *p = (const unsigned char *)str;
    while (*p && count < max_len) {
        uint32_t cp = 0;
        if ((*p & 0x80) == 0) {
            cp = *p++;
        } else if ((*p & 0xE0) == 0xC0) {
            cp = (*p++ & 0x1F) << 6;
            if (*p) cp |= (*p++ & 0x3F);
        } else if ((*p & 0xF0) == 0xE0) {
            cp = (*p++ & 0x0F) << 12;
            if (*p) cp |= (*p++ & 0x3F) << 6;
            if (*p) cp |= (*p++ & 0x3F);
        } else {
            p++;
            continue;
        }
        masks[count++] = char_to_mask(cp);
    }
    return count;
}

/* Команда: показать 4 символа (статично) */
static void cmd_show(const char *text, int colon, int minus, int degree) {
    uint8_t masks[16] = {0};
    int len = parse_utf8_to_masks(text, masks, 16);
    uint8_t d1 = (len > 0) ? masks[0] : 0;
    uint8_t d2 = (len > 1) ? masks[1] : 0;
    uint8_t d3 = (len > 2) ? masks[2] : 0;
    uint8_t d4 = (len > 3) ? masks[3] : 0;
    render_raw(d1, d2, d3, d4, colon, minus, degree, default_brightness);
}

/* Команда: бегущая строка (ticker) */
static void cmd_scroll(const char *text, int speed_ms, int loops) {
    uint8_t raw[128] = {0};
    int raw_len = parse_utf8_to_masks(text, raw, 120);
    if (raw_len == 0) return;

    /* Добавляем 4 пустых сегмента спереди и сзади для плавного входа/выхода */
    uint8_t buf[256] = {0};
    int pad = 4;
    for (int i = 0; i < raw_len; ++i) {
        buf[pad + i] = raw[i];
    }
    int total = pad + raw_len + pad;
    int interval_us = (speed_ms > 0 ? speed_ms : 180) * 1000;
    int loop_count = (loops > 0) ? loops : 1;

    for (int l = 0; l < loop_count && keep_running; ++l) {
        for (int i = 0; i <= total - 4 && keep_running; ++i) {
            render_raw(buf[i], buf[i+1], buf[i+2], buf[i+3], 0, 0, 0, default_brightness);
            usleep(interval_us);
        }
    }
    if (keep_running) {
        cmd_clock(1);
    }
}

/* Команда: температура (+21 или -5) */
static void cmd_temp(const char *val_str, int duration_secs) {
    int val = atoi(val_str);
    int is_neg = (val < 0) || (val_str[0] == '-');
    int abs_val = abs(val);

    uint8_t d1 = 0, d2 = 0, d3 = 0, d4 = 0;
    if (abs_val >= 100) {
        d1 = char_to_mask('0' + (abs_val / 100) % 10);
        d2 = char_to_mask('0' + (abs_val / 10) % 10);
        d3 = char_to_mask('0' + abs_val % 10);
    } else if (abs_val >= 10) {
        d2 = char_to_mask('0' + (abs_val / 10) % 10);
        d3 = char_to_mask('0' + abs_val % 10);
    } else {
        d3 = char_to_mask('0' + abs_val % 10);
    }

    render_raw(d1, d2, d3, d4, 0, is_neg, 1, default_brightness);
    if (duration_secs > 0) {
        sleep(duration_secs);
        if (keep_running) cmd_clock(1);
    }
}

/* Команда: рожицы и эмоции */
static void cmd_face(const char *name, int duration_secs) {
    uint64_t end_ms = (duration_secs > 0) ? ((uint64_t)time(NULL) + duration_secs) * 1000 : 0;

    if (strcmp(name, "happy") == 0) {
        /* ^  ^ */
        uint8_t eye = SEG_A | SEG_B | SEG_F;
        render_raw(0, eye, eye, 0, 0, 0, 0, default_brightness);
        if (duration_secs > 0) sleep(duration_secs);
    } else if (strcmp(name, "sleep") == 0) {
        /* -  - */
        uint8_t eye = SEG_G;
        render_raw(0, eye, eye, 0, 0, 0, 0, default_brightness / 2);
        if (duration_secs > 0) sleep(duration_secs);
    } else if (strcmp(name, "cool") == 0 || strcmp(name, "wide") == 0) {
        /* O  O */
        uint8_t eye = SEG_A | SEG_B | SEG_C | SEG_D | SEG_E | SEG_F;
        render_raw(0, eye, eye, 0, 0, 0, 0, default_brightness);
        if (duration_secs > 0) sleep(duration_secs);
    } else if (strcmp(name, "wink") == 0) {
        /* ^  - */
        uint8_t e1 = SEG_A | SEG_B | SEG_F;
        uint8_t e2 = SEG_G;
        render_raw(0, e1, e2, 0, 0, 0, 0, default_brightness);
        if (duration_secs > 0) sleep(duration_secs);
    } else if (strcmp(name, "love") == 0) {
        /* <  3 */
        uint8_t d2 = SEG_E | SEG_D | SEG_G;
        uint8_t d3 = SEG_A | SEG_B | SEG_G | SEG_C | SEG_D;
        render_raw(0, d2, d3, 0, 0, 0, 0, default_brightness);
        if (duration_secs > 0) sleep(duration_secs);
    } else if (strcmp(name, "talk") == 0 || strcmp(name, "speaking") == 0) {
        /* Анимированный говорящий рот между глаз: ^ __ ^ -> ^ oo ^ -> ^ OO ^ */
        uint8_t eye = SEG_A | SEG_B | SEG_F;
        static const uint8_t mouths[4] = {
            SEG_D,                   /* _ */
            SEG_G,                   /* - */
            SEG_C | SEG_D | SEG_E | SEG_G, /* o */
            SEG_A | SEG_B | SEG_C | SEG_D | SEG_E | SEG_F /* O */
        };
        int idx = 0;
        while (keep_running) {
            uint8_t m = mouths[idx % 4];
            render_raw(eye, m, m, eye, 0, 0, 0, default_brightness);
            usleep(140000);
            idx++;
            if (end_ms > 0) {
                struct timespec ts;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                if (((uint64_t)time(NULL) * 1000) >= end_ms) break;
            }
        }
    } else if (strcmp(name, "think") == 0 || strcmp(name, "spinner") == 0) {
        /* Крутящийся индикатор загрузки по кругу цифр 2 и 3 */
        static const uint8_t spin[6] = {
            SEG_A, SEG_B, SEG_C, SEG_D, SEG_E, SEG_F
        };
        int s = 0;
        while (keep_running) {
            uint8_t m = spin[s % 6];
            render_raw(0, m, m, 0, 0, 0, 0, default_brightness);
            usleep(100000);
            s++;
            if (end_ms > 0 && ((uint64_t)time(NULL) * 1000) >= end_ms) break;
        }
    } else if (strcmp(name, "dizzy") == 0) {
        /* Вращающиеся сегменты в глазах */
        static const uint8_t seq[4] = {
            SEG_A | SEG_D,
            SEG_B | SEG_E,
            SEG_G,
            SEG_C | SEG_F
        };
        int s = 0;
        while (keep_running) {
            uint8_t m = seq[s % 4];
            render_raw(0, m, m, 0, 0, 0, 0, default_brightness);
            usleep(110000);
            s++;
            if (end_ms > 0 && ((uint64_t)time(NULL) * 1000) >= end_ms) break;
        }
    }
    if (duration_secs > 0 && keep_running) {
        cmd_clock(1);
    }
}

/* Команда: эквалайзер / аудио-волна */
static void cmd_equalizer(int seconds) {
    /* 4 столбца с высотой от 0 до 3:
     * 0: пусто
     * 1: нижняя черта SEG_D
     * 2: нижняя + средняя SEG_D | SEG_G
     * 3: полная высота SEG_D | SEG_G | SEG_A
     */
    static const uint8_t levels[4] = {
        0,
        SEG_D,
        SEG_D | SEG_G,
        SEG_D | SEG_G | SEG_A
    };

    uint64_t end_ms = (seconds > 0) ? ((uint64_t)time(NULL) + seconds) * 1000 : 0;
    srand(time(NULL));

    int h[4] = {1, 2, 3, 2};
    while (keep_running) {
        for (int i = 0; i < 4; ++i) {
            int delta = (rand() % 3) - 1; /* -1, 0, +1 */
            h[i] += delta;
            if (h[i] < 1) h[i] = 1;
            if (h[i] > 3) h[i] = 3;
        }
        render_raw(levels[h[0]], levels[h[1]], levels[h[2]], levels[h[3]], 0, 0, 0, default_brightness);
        usleep(90000);
        if (end_ms > 0 && ((uint64_t)time(NULL) * 1000) >= end_ms) break;
    }

    if (seconds > 0 && keep_running) {
        cmd_clock(1);
    }
}

/* Команда: таймер с обратным отсчётом */
static void cmd_timer(int total_secs) {
    for (int rem = total_secs; rem >= 0 && keep_running; --rem) {
        int d1, d2, d3, d4;
        if (rem >= 3600) {
            int hrs = rem / 3600;
            int mins = (rem % 3600) / 60;
            d1 = hrs / 10;
            d2 = hrs % 10;
            d3 = mins / 10;
            d4 = mins % 10;
        } else {
            int mins = rem / 60;
            int secs = rem % 60;
            d1 = mins / 10;
            d2 = mins % 10;
            d3 = secs / 10;
            d4 = secs % 10;
        }
        int colon = (rem % 2 == 0); /* мигающее двоеточие */
        render_raw(char_to_mask('0' + d1), char_to_mask('0' + d2),
                   char_to_mask('0' + d3), char_to_mask('0' + d4),
                   colon, 0, 0, default_brightness);
        sleep(1);
    }

    /* Мигаем 00:00 в конце */
    uint8_t z = char_to_mask('0');
    for (int i = 0; i < 4 && keep_running; ++i) {
        render_raw(0, 0, 0, 0, 0, 0, 0, 0);
        usleep(250000);
        render_raw(z, z, z, z, 1, 0, 0, default_brightness);
        usleep(350000);
    }
}

/* Команда: часы (HH:MM) */
static void cmd_clock(int seconds) {
    uint64_t end_ms = (seconds > 0) ? ((uint64_t)time(NULL) + seconds) * 1000 : 0;
    while (keep_running) {
        time_t now = time(NULL);
        struct tm *tm = localtime(&now);
        int hrs = tm->tm_hour;
        int mins = tm->tm_min;
        int secs = tm->tm_sec;

        uint8_t d1 = (hrs >= 10) ? char_to_mask('0' + hrs / 10) : 0;
        uint8_t d2 = char_to_mask('0' + hrs % 10);
        uint8_t d3 = char_to_mask('0' + mins / 10);
        uint8_t d4 = char_to_mask('0' + mins % 10);

        int colon = (secs % 2 == 0);
        render_raw(d1, d2, d3, d4, colon, 0, 0, default_brightness);
        usleep(500000);
        if (end_ms > 0 && ((uint64_t)time(NULL) * 1000) >= end_ms) break;
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        printf("Использование: display-tool <команда> [параметры...]\n"
               "Команды:\n"
               "  show <текст>               Показать 4 символа (статично)\n"
               "  scroll <текст> [мс] [раз]  Бегущая строка (UTF-8, русский/латиница)\n"
               "  temp <число>               Показать температуру (напр. +21 или -5)\n"
               "  face <имя> [сек]           Эмоция: happy, sleep, cool, wink, love, talk, think, dizzy\n"
               "  eq [сек]                   Анимированный эквалайзер\n"
               "  timer <секунды>            Таймер обратного отсчёта\n"
               "  clock [сек]                Отображение текущего времени\n"
               "  brightness <1..255>        Установить и сохранить яркость\n"
               "  clear                      Погасить все сегменты и индикаторы\n");
        return 0;
    }

    signal(SIGINT, handle_sig);
    signal(SIGTERM, handle_sig);

    const char *cmd = argv[1];

    if (strcmp(cmd, "brightness") == 0) {
        if (argc >= 3) {
            int b = atoi(argv[2]);
            if (b >= 1 && b <= 255) {
                FILE *fp = fopen(BRIGHTNESS_FILE, "w");
                if (fp) {
                    fprintf(fp, "%d\n", b);
                    fclose(fp);
                }
                printf("Яркость установлена на %d\n", b);
            }
        }
        return 0;
    }

    stop_other_instances();
    record_pid();
    init_leds();

    if (strcmp(cmd, "clear") == 0) {
        clear_display();
    } else if (strcmp(cmd, "show") == 0) {
        const char *txt = (argc >= 3) ? argv[2] : "";
        int colon = (strchr(txt, ':') != NULL);
        cmd_show(txt, colon, 0, 0);
    } else if (strcmp(cmd, "scroll") == 0) {
        const char *txt = (argc >= 3) ? argv[2] : "";
        int speed = (argc >= 4) ? atoi(argv[3]) : 180;
        int loops = (argc >= 5) ? atoi(argv[4]) : 1;
        cmd_scroll(txt, speed, loops);
    } else if (strcmp(cmd, "temp") == 0) {
        const char *val = (argc >= 3) ? argv[2] : "0";
        int secs = (argc >= 4) ? atoi(argv[3]) : 6;
        cmd_temp(val, secs);
    } else if (strcmp(cmd, "face") == 0) {
        const char *face = (argc >= 3) ? argv[2] : "happy";
        int secs = (argc >= 4) ? atoi(argv[3]) : 0;
        cmd_face(face, secs);
    } else if (strcmp(cmd, "eq") == 0) {
        int secs = (argc >= 3) ? atoi(argv[2]) : 0;
        cmd_equalizer(secs);
    } else if (strcmp(cmd, "timer") == 0) {
        int secs = (argc >= 3) ? atoi(argv[2]) : 60;
        cmd_timer(secs);
    } else if (strcmp(cmd, "clock") == 0) {
        int secs = (argc >= 3) ? atoi(argv[2]) : 0;
        cmd_clock(secs);
    } else {
        fprintf(stderr, "Неизвестная команда: %s\n", cmd);
    }

    unlink(PID_FILE);
    close_leds();
    return 0;
}
