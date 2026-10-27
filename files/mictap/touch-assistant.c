/*
 * touch-assistant — демон голосового ассистента Qwen для Яндекс Станции Мини 2:
 *
 * 1. Активация голосом («Алиса, ...»):
 *    - Получает "WAKE" из /tmp/spotter.sock от mictap.so (перехват NPU-споттера).
 *    - Хранит кольцевой пред-буфер 0.8 с из /tmp/vqe.sock, поэтому при слитной
 *      фразе («Алиса, который час?») начало вопроса не обрезается.
 *    - Динамический VAD: ждёт начала речи и завершает запись через 0.85 с тишины
 *      после окончания фразы (до 9 с максимум).
 *
 * 2. Активация зажатием центральной кнопки (code=582, удержание >= 350 мс):
 *    - При удержании >= 350 мс включает запись (работает и пока держишь кнопку,
 *      и до конца фразы по VAD после отпускания).
 *
 * 3. Короткое нажатие центральной кнопки (code=582, < 350 мс) — «Заткнуться»:
 *    - Если колонка слушает, думает (curl) или говорит (aplay) — мгновенно
 *      прерывает запись/ответ, убивает aplay/curl и гасит кольцо.
 *    - В режиме покоя короткое нажатие ничего не запускает.
 *
 * 4. Громкость и индикация:
 *    - Не использует EVIOCGRAB: кнопки громкости (114/115) и часы работают штатно.
 *    - SPK Master всегда 255 (0.0 dB), громкость ответов масштабируется по Master.
 *    - При выключении анимации принудительно гасит все 18 светодиодов кольца.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define KEY_VOLUMEDOWN_CODE   114
#define KEY_VOLUMEUP_CODE     115
#define KEY_VOICECOMMAND_CODE 582

#define SAMPLE_RATE       16000
#define BYTES_PER_SEC     (SAMPLE_RATE * 2)
#define PREBUF_BYTES      25600               /* 0.8 с кольцевого пред-буфера */
#define MAX_RECORD_SECS   7
#define MAX_PCM_BYTES     (BYTES_PER_SEC * MAX_RECORD_SECS)

#define LONG_PRESS_MS     350                 /* порог зажатия кнопки (мс) */
#define MIN_RECORD_MS     800                 /* мин. время записи после старта */
#define NO_SPEECH_MS      2600                /* таймаут тишины, если речь не началась */
#define END_SILENCE_MS    520                 /* пауза после речи для авто-стопа VAD */
#define SPEECH_RMS_THRESH 95                  /* порог средней амплитуды речи (с учётом +24 dB в mictap) */

#define TRIGGER_WAKEWORD  1
#define TRIGGER_HOLD      2

struct wav_header {
    char     riff[4];
    uint32_t overall_size;
    char     wave[4];
    char     fmt_chunk_marker[4];
    uint32_t length_of_fmt;
    uint16_t format_type;
    uint16_t channels;
    uint32_t sample_rate;
    uint32_t byterate;
    uint16_t block_align;
    uint16_t bits_per_sample;
    char     data_chunk_header[4];
    uint32_t data_size;
} __attribute__((packed));

static volatile sig_atomic_t keep_running = 1;
static pid_t led_pid = -1;
static pid_t async_audio_pid = -1;

static uint8_t prebuf[PREBUF_BYTES];
static size_t prebuf_pos = 0;
static size_t prebuf_filled = 0;
static char g_server_url[256] = "http://192.168.2.85:8730";

static void handle_sig(int sig) {
    (void)sig;
    keep_running = 0;
}

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000ULL);
}

static void clear_ring_leds(void) {
    static const char *colors[3] = {"red", "green", "blue"};
    char path[128];
    for (int c = 0; c < 3; ++c) {
        for (int r = 1; r <= 6; ++r) {
            snprintf(path, sizeof(path),
                     "/sys/class/leds/mini2:%s:ring%d/brightness",
                     colors[c], r);
            int fd = open(path, O_WRONLY | O_CLOEXEC);
            if (fd >= 0) {
                if (write(fd, "0\n", 2) < 0) {}
                close(fd);
            }
        }
    }
}

static void stop_led(void) {
    if (led_pid > 0) {
        kill(led_pid, SIGKILL);
        waitpid(led_pid, NULL, 0);
        led_pid = -1;
    }
    clear_ring_leds();
}

static void write_led_val(int fd, unsigned int val) {
    if (fd < 0) return;
    char buf[16];
    int n = snprintf(buf, sizeof(buf), "%u\n", val & 0xff);
    if (pwrite(fd, buf, (size_t)n, 0) < 0) {}
}

static void run_led_pattern_loop(const char *pattern_path) {
    static const char *colors[3] = {"red", "green", "blue"};
    int fds[6][3];
    char path[128];
    for (int r = 0; r < 6; ++r) {
        for (int c = 0; c < 3; ++c) {
            snprintf(path, sizeof(path),
                     "/sys/class/leds/mini2:%s:ring%d/brightness",
                     colors[c], r + 1);
            fds[r][c] = open(path, O_WRONLY | O_CLOEXEC);
        }
    }

    FILE *fp = pattern_path ? fopen(pattern_path, "r") : NULL;
    if (fp) {
        char line[256];
        int do_loop = 1;
        for (;;) {
            rewind(fp);
            int frames = 0;
            while (fgets(line, sizeof(line), fp)) {
                if (strncmp(line, "loop", 4) == 0) {
                    do_loop = 1;
                    continue;
                }
                unsigned int z[6], ms = 25;
                if (sscanf(line, "%x %x %x %x %x %x %u",
                           &z[0], &z[1], &z[2], &z[3], &z[4], &z[5], &ms) == 7) {
                    for (int r = 0; r < 6; ++r) {
                        write_led_val(fds[r][0], (z[r] >> 16) & 0xff);
                        write_led_val(fds[r][1], (z[r] >> 8) & 0xff);
                        write_led_val(fds[r][2], z[r] & 0xff);
                    }
                    if (ms > 1000) ms = 1000;
                    usleep(ms * 1000);
                    frames++;
                }
            }
            if (!do_loop || frames == 0) break;
        }
        fclose(fp);
    }

    /* Встроенная плавная фиолетово-голубая анимация кольца, если файл не найден */
    int step = 0;
    for (;;) {
        for (int r = 0; r < 6; ++r) {
            int dist = (r - (step % 6) + 6) % 6;
            unsigned int red = (dist == 0) ? 160 : (dist == 1 || dist == 5) ? 80 : 25;
            unsigned int grn = (dist == 0) ? 30  : 5;
            unsigned int blu = (dist == 0) ? 255 : (dist == 1 || dist == 5) ? 160 : 60;
            write_led_val(fds[r][0], red);
            write_led_val(fds[r][1], grn);
            write_led_val(fds[r][2], blu);
        }
        usleep(70000);
        step++;
    }
}

static void start_led(const char *pattern_path) {
    stop_led();
    pid_t pid = fork();
    if (pid == 0) {
        run_led_pattern_loop(pattern_path);
        _exit(0);
    } else if (pid > 0) {
        led_pid = pid;
    }
}

static void stop_pid_gracefully(pid_t pid) {
    if (pid <= 0) return;
    kill(pid, SIGTERM);
    for (int i = 0; i < 8; ++i) {
        if (waitpid(pid, NULL, WNOHANG) == pid) {
            return;
        }
        usleep(15000);
    }
    kill(pid, SIGKILL);
    waitpid(pid, NULL, 0);
}

static void stop_async_audio(void) {
    if (async_audio_pid > 0) {
        stop_pid_gracefully(async_audio_pid);
        async_audio_pid = -1;
    }
}

static void reap_async_audio(void) {
    if (async_audio_pid > 0) {
        int st = 0;
        if (waitpid(async_audio_pid, &st, WNOHANG) > 0) {
            async_audio_pid = -1;
        }
    }
}

static void ensure_alsa_free(void) {
    FILE *fp = fopen("/proc/asound/card0/pcm0p/sub0/status", "r");
    if (!fp) return;
    char buf[256] = {0};
    size_t n = fread(buf, 1, sizeof(buf) - 1, fp);
    fclose(fp);
    if (n == 0 || strstr(buf, "closed") != NULL) return;

    /* Если карту держит shairport-sync или aplay через dmix, убивать mediad не нужно */
    char *p = strstr(buf, "owner_pid");
    if (p) {
        int owner = -1;
        if (sscanf(p, "owner_pid : %d", &owner) == 1 && owner > 0) {
            char comm_path[64], comm[64] = {0};
            snprintf(comm_path, sizeof(comm_path), "/proc/%d/comm", owner);
            FILE *cf = fopen(comm_path, "r");
            if (cf) {
                if (fgets(comm, sizeof(comm), cf)) {}
                fclose(cf);
                if (strstr(comm, "shairport") != NULL || strstr(comm, "aplay") != NULL) {
                    return;
                }
            }
        }
    }

    if (access("/etc/airplay-free.sh", X_OK) == 0) {
        if (system("/etc/airplay-free.sh >/dev/null 2>&1") < 0) {}
    }
}

/* Значение микшера по имени контрола (0..255) или -1, если прочитать не удалось. */
static int read_mixer_value(const char *control) {
    char cmd[128];
    snprintf(cmd, sizeof(cmd), "amixer sget '%s' 2>/dev/null", control);
    FILE *fp = popen(cmd, "r");
    if (!fp) return -1;
    char line[256];
    int val = -1;
    while (fgets(line, sizeof(line), fp)) {
        if (strstr(line, "Limits:")) continue;
        char *p = strstr(line, "Playback ");
        if (p) {
            int v = -1;
            if (sscanf(p, "Playback %d", &v) == 1 && v >= 0 && v <= 255) {
                val = v;
                break;
            }
        }
    }
    pclose(fp);
    return val;
}

/*
 * SPK Master — единственный контрол, который реально влияет на звук:
 * его же пишет shairport-sync, когда громкость меняют с Мака.
 * Безопасный комфортный диапазон: 100% ограничен 239 (-8 dB), колонка никогда не орёт.
 * 30% (195, -30 dB) — слышимый приятный фон, 50% (211, -22 dB) — стандартный средний.
 */
static const int SPK_VOL_MAP[11] = {
    0,   /* 0: mute */
    175, /* 1: 10% (-40.0 dB) тихий ночной */
    185, /* 2: 20% (-35.0 dB) тихий вечерний */
    195, /* 3: 30% (-30.0 dB) комфортный фоновый */
    203, /* 4: 40% (-26.0 dB) мягкий */
    211, /* 5: 50% (-22.0 dB) стандартный средний */
    217, /* 6: 60% (-19.0 dB) */
    223, /* 7: 70% (-16.0 dB) уверенно громкий без перегруза */
    229, /* 8: 80% (-13.0 dB) */
    234, /* 9: 90% (-10.5 dB) */
    239  /* 10: 100% (-8.0 dB) максимум (безопасный потолок, не орёт) */
};

static int g_cur_vol_level = -1;

static int get_spk_volume(void) {
    int val = read_mixer_value("SPK Master");
    return (val >= 0) ? val : SPK_VOL_MAP[5];
}

static int spk_to_level(int spk) {
    int best = 1, best_diff = 1024;
    for (int i = 1; i <= 10; ++i) {
        int d = spk - SPK_VOL_MAP[i];
        if (d < 0) d = -d;
        if (d < best_diff) {
            best_diff = d;
            best = i;
        }
    }
    return best;
}

/*
 * Приводим «громкость Алисы» (Master) к реальному уровню SPK Master,
 * чтобы индикатор на колонке не показывал ноль во время AirPlay.
 */
static void sync_hardware_volume(void) {
    int spk = read_mixer_value("SPK Master");
    int lvl = (spk >= 0) ? spk_to_level(spk) : 5;
    g_cur_vol_level = lvl;
    int master_val = lvl * 25;
    if (master_val > 255) master_val = 255;
    char cmd[96];
    snprintf(cmd, sizeof(cmd), "amixer sset 'Master' %d >/dev/null 2>&1", master_val);
    if (system(cmd) < 0) {}
}

static void play_wav_async(const char *wav_path) {
    stop_async_audio();
    if (access(wav_path, R_OK) != 0) return;
    int vol = get_spk_volume();
    if (vol <= 0) return;

    pid_t pid = fork();
    if (pid == 0) {
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            if (devnull > 2) close(devnull);
        }
        execl("/usr/bin/aplay", "aplay", "-q", "-D", "dmix_spk_plug", wav_path, (char *)NULL);
        _exit(127);
    } else if (pid > 0) {
        async_audio_pid = pid;
    }
}

static void adjust_station_volume(int delta) {
    int spk = get_spk_volume();
    int hw_lvl = spk_to_level(spk);
    int lvl = (g_cur_vol_level >= 1 && g_cur_vol_level <= 10) ? g_cur_vol_level : hw_lvl;
    /* Если громкость заметно изменили снаружи (с Мака по AirPlay), отталкиваемся от SPK Master */
    int expected_spk = SPK_VOL_MAP[lvl];
    int diff = spk - expected_spk;
    if (diff < 0) diff = -diff;
    if (diff > 4) {
        lvl = hw_lvl;
    }
    lvl += delta;
    if (lvl > 10) lvl = 10;
    if (lvl < 1) lvl = 1;
    g_cur_vol_level = lvl;

    int new_spk = SPK_VOL_MAP[lvl];
    int master_val = lvl * 25;
    if (master_val > 255) master_val = 255;
    fprintf(stderr, "[touch-assistant] Громкость: delta=%d -> уровень=%d (SPK=%d, Master=%d)\n",
            delta, lvl, new_spk, master_val);
    char cmd[160];
    snprintf(cmd, sizeof(cmd),
             "amixer sset 'SPK Master' %d >/dev/null 2>&1; "
             "amixer sset 'Master' %d >/dev/null 2>&1",
             new_spk, master_val);
    if (system(cmd) < 0) {}
    char notify_cmd[512];
    snprintf(notify_cmd, sizeof(notify_cmd),
             "/opt/bin/curl -sS -m 2 -X POST '%s/music/volume?level=%d' >/dev/null 2>&1 &",
             g_server_url, lvl);
    if (system(notify_cmd) < 0) {}
    play_wav_async("/data/mictap/vol_click.wav");
}

static void prebuf_push(const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; ++i) {
        prebuf[prebuf_pos] = data[i];
        prebuf_pos = (prebuf_pos + 1) % PREBUF_BYTES;
    }
    if (prebuf_filled + len >= PREBUF_BYTES) {
        prebuf_filled = PREBUF_BYTES;
    } else {
        prebuf_filled += len;
    }
}

static size_t prebuf_copy_latest(uint8_t *dst, size_t want_bytes) {
    if (want_bytes > prebuf_filled) {
        want_bytes = prebuf_filled;
    }
    want_bytes &= ~(size_t)1; /* чётное число байт (S16_LE) */
    if (want_bytes == 0) return 0;

    size_t start = (prebuf_pos + PREBUF_BYTES - want_bytes) % PREBUF_BYTES;
    for (size_t i = 0; i < want_bytes; ++i) {
        dst[i] = prebuf[(start + i) % PREBUF_BYTES];
    }
    return want_bytes;
}

static int write_wav_file(const char *path, const uint8_t *pcm, uint32_t pcm_bytes) {
    struct wav_header hdr;
    memcpy(hdr.riff, "RIFF", 4);
    hdr.overall_size = pcm_bytes + sizeof(struct wav_header) - 8;
    memcpy(hdr.wave, "WAVE", 4);
    memcpy(hdr.fmt_chunk_marker, "fmt ", 4);
    hdr.length_of_fmt = 16;
    hdr.format_type = 1;
    hdr.channels = 1;
    hdr.sample_rate = SAMPLE_RATE;
    hdr.byterate = SAMPLE_RATE * 2;
    hdr.block_align = 2;
    hdr.bits_per_sample = 16;
    memcpy(hdr.data_chunk_header, "data", 4);
    hdr.data_size = pcm_bytes;

    FILE *fp = fopen(path, "wb");
    if (!fp) return -1;
    fwrite(&hdr, 1, sizeof(hdr), fp);
    if (pcm_bytes > 0) {
        fwrite(pcm, 1, pcm_bytes, fp);
    }
    fclose(fp);
    return 0;
}

static int frame_mean_abs(const uint8_t *buf, size_t nbytes) {
    size_t nsamples = nbytes / 2;
    if (nsamples == 0) return 0;
    const int16_t *s = (const int16_t *)buf;
    uint32_t sum = 0;
    for (size_t i = 0; i < nsamples; ++i) {
        int32_t v = s[i];
        sum += (uint32_t)(v < 0 ? -v : v);
    }
    return (int)(sum / nsamples);
}

static void drain_spotter_sock(int spot_fd, int *got_wake, int *got_stop,
                               int *got_play, int *got_next,
                               int *got_volup, int *got_voldown) {
    char pkt[64];
    for (;;) {
        ssize_t n = recv(spot_fd, pkt, sizeof(pkt) - 1, MSG_DONTWAIT);
        if (n <= 0) break;
        pkt[n] = '\0';
        if (strstr(pkt, "STOP")) {
            if (got_stop) *got_stop = 1;
        } else if (strstr(pkt, "WAKE")) {
            if (got_wake) *got_wake = 1;
        } else if (strstr(pkt, "PLAY")) {
            if (got_play) *got_play = 1;
        } else if (strstr(pkt, "NEXT")) {
            if (got_next) *got_next = 1;
        } else if (strstr(pkt, "VOLUP")) {
            if (got_volup) *got_volup = 1;
        } else if (strstr(pkt, "VOLDOWN")) {
            if (got_voldown) *got_voldown = 1;
        }
    }
}

static void drain_vqe_to_prebuf(int vqe_fd) {
    uint8_t pkt[2048];
    for (;;) {
        ssize_t n = recv(vqe_fd, pkt, sizeof(pkt), MSG_DONTWAIT);
        if (n <= 0) break;
        prebuf_push(pkt, (size_t)n);
    }
}

/*
 * Запись голоса с динамическим VAD и поддержкой отмены коротким нажатием кнопки:
 * Возвращает:
 *   > 0 : число записанных байт PCM
 *   -1  : запись отменена пользователем (короткое нажатие кнопки или «Стоп»)
 */
static ssize_t record_dialog(int touch_fd, int vqe_fd, int spot_fd,
                             uint8_t *pcm_buf, size_t max_bytes,
                             int trigger_type, int *btn_is_down) {
    size_t total = 0;
    if (trigger_type == TRIGGER_WAKEWORD) {
        /* Берём последние 0.65 с из пред-буфера, чтобы слитная фраза не обрезалась */
        total = prebuf_copy_latest(pcm_buf, 20800);
    } else {
        /* При зажатии кнопки берём 0.35 с с момента касания */
        total = prebuf_copy_latest(pcm_buf, 11200);
    }

    int holding_initial = (trigger_type == TRIGGER_HOLD && *btn_is_down) ? 1 : 0;
    uint64_t start_ms = now_ms();
    uint64_t last_speech_ms = start_ms;
    int speech_frames = 0;

    while (keep_running && total < max_bytes) {
        reap_async_audio();
        uint64_t elapsed = now_ms() - start_ms;
        if (elapsed >= (uint64_t)(MAX_RECORD_SECS * 1000)) {
            break;
        }

        /* Если кнопка не удерживается, проверяем условия авто-завершения по VAD */
        if (!holding_initial) {
            if (speech_frames >= 8 && elapsed >= MIN_RECORD_MS &&
                (now_ms() - last_speech_ms) >= END_SILENCE_MS) {
                break;
            }
            if (speech_frames < 8 && elapsed >= NO_SPEECH_MS) {
                break;
            }
        }

        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(vqe_fd, &rfds);
        FD_SET(touch_fd, &rfds);
        FD_SET(spot_fd, &rfds);
        int maxfd = vqe_fd;
        if (touch_fd > maxfd) maxfd = touch_fd;
        if (spot_fd > maxfd) maxfd = spot_fd;

        struct timeval tv;
        tv.tv_sec = 0;
        tv.tv_usec = 30000; /* 30 мс */

        int r = select(maxfd + 1, &rfds, NULL, NULL, &tv);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }

        if (r > 0 && FD_ISSET(spot_fd, &rfds)) {
            int wake = 0, stop = 0, volup = 0, voldown = 0;
            drain_spotter_sock(spot_fd, &wake, &stop, NULL, NULL, &volup, &voldown);
            if (stop) {
                fprintf(stderr, "[touch-assistant] Отмена голосом (Стоп/Хватит)\n");
                return -1;
            }
            /*
             * «Алиса» приходит отдельным событием и запускает диалог раньше,
             * чем споттер распознает «громче»/«тише». Поэтому команду громкости
             * надо обрабатывать и внутри записи, иначе она теряется.
             */
            if (volup) {
                fprintf(stderr, "[touch-assistant] Голосом во время слушания: «громче»\n");
                adjust_station_volume(1);
                return -2;
            }
            if (voldown) {
                fprintf(stderr, "[touch-assistant] Голосом во время слушания: «тише»\n");
                adjust_station_volume(-1);
                return -2;
            }
        }

        if (r > 0 && FD_ISSET(touch_fd, &rfds)) {
            struct input_event ev;
            while (read(touch_fd, &ev, sizeof(ev)) == sizeof(ev)) {
                if (ev.type == EV_KEY && ev.code == KEY_VOICECOMMAND_CODE) {
                    if (ev.value == 0) {
                        *btn_is_down = 0;
                        if (holding_initial) {
                            holding_initial = 0;
                            last_speech_ms = now_ms();
                        }
                    } else if (ev.value == 1) {
                        *btn_is_down = 1;
                        if (!holding_initial) {
                            /* Нажатие кнопки во время слушания = ЗАТКНУТЬСЯ (отмена)! */
                            fprintf(stderr, "[touch-assistant] Нажатие кнопки во время слушания — отмена!\n");
                            return -1;
                        }
                    }
                } else if (ev.type == EV_KEY && ev.value == 1 &&
                           (ev.code == KEY_VOLUMEUP_CODE || ev.code == KEY_VOLUMEDOWN_CODE)) {
                    if (ev.code == KEY_VOLUMEUP_CODE) {
                        adjust_station_volume(1);
                    } else {
                        adjust_station_volume(-1);
                    }
                }
            }
        }

        if (r > 0 && FD_ISSET(vqe_fd, &rfds)) {
            uint8_t pkt[2048];
            for (;;) {
                ssize_t n = recv(vqe_fd, pkt, sizeof(pkt), MSG_DONTWAIT);
                if (n <= 0) break;
                prebuf_push(pkt, (size_t)n);

                size_t space = max_bytes - total;
                size_t copy = ((size_t)n < space) ? (size_t)n : space;
                memcpy(pcm_buf + total, pkt, copy);
                total += copy;

                /* Считаем энергию речи после первых 250 мс (когда стих сигнал старта) */
                if ((now_ms() - start_ms) >= 250) {
                    int m = frame_mean_abs(pkt, (size_t)n);
                    if (m >= SPEECH_RMS_THRESH) {
                        speech_frames++;
                        last_speech_ms = now_ms();
                    }
                }
            }
        }
    }

    return (ssize_t)total;
}

/*
 * Выполняет дочерний процесс (curl или aplay), одновременно слушая кнопку (582)
 * и команду "STOP" из /tmp/spotter.sock.
 * Возвращает:
 *    0 : процесс завершился штатно
 *   -1 : прервано коротким нажатием кнопки или голосом «Стоп»
 *   -2 : прервано новым зажатием кнопки (>= 350 мс) — сразу начать новый диалог!
 */
static int run_interruptible_pid(pid_t child_pid, int touch_fd, int vqe_fd, int spot_fd,
                                 int *btn_is_down) {
    int interrupted = 0;
    uint64_t press_ms = 0;

    while (keep_running) {
        drain_vqe_to_prebuf(vqe_fd);

        int status = 0;
        pid_t w = waitpid(child_pid, &status, WNOHANG);
        if (w == child_pid) {
            child_pid = -1;
            break;
        }

        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(touch_fd, &rfds);
        FD_SET(spot_fd, &rfds);
        FD_SET(vqe_fd, &rfds);
        int maxfd = touch_fd;
        if (spot_fd > maxfd) maxfd = spot_fd;
        if (vqe_fd > maxfd) maxfd = vqe_fd;

        struct timeval tv;
        tv.tv_sec = 0;
        tv.tv_usec = 25000; /* 25 мс */

        int r = select(maxfd + 1, &rfds, NULL, NULL, &tv);
        if (r > 0 && FD_ISSET(spot_fd, &rfds)) {
            int wake = 0, stop = 0, volup = 0, voldown = 0;
            drain_spotter_sock(spot_fd, &wake, &stop, NULL, NULL, &volup, &voldown);
            if (volup) {
                fprintf(stderr, "[touch-assistant] Голосом во время звука: «громче»\n");
                adjust_station_volume(1);
            } else if (voldown) {
                fprintf(stderr, "[touch-assistant] Голосом во время звука: «тише»\n");
                adjust_station_volume(-1);
            } else if (wake || stop) {
                fprintf(stderr, "[touch-assistant] Остановка по голосовой команде (%s)!\n",
                        stop ? "Стоп" : "Алиса");
                stop_pid_gracefully(child_pid);
                if (wake) {
                    return -2;
                }
                return -1;
            }
        }

        if (r > 0 && FD_ISSET(touch_fd, &rfds)) {
            struct input_event ev;
            while (read(touch_fd, &ev, sizeof(ev)) == sizeof(ev)) {
                if (ev.type == EV_KEY && ev.code == KEY_VOICECOMMAND_CODE) {
                    if (ev.value == 1) {
                        *btn_is_down = 1;
                        press_ms = now_ms();
                        interrupted = 1;
                        /* Мгновенно затыкаем текущий процесс при касании кнопки! */
                        stop_pid_gracefully(child_pid);
                        child_pid = -1;
                        stop_led();
                        break;
                    } else if (ev.value == 0) {
                        *btn_is_down = 0;
                    }
                } else if (ev.type == EV_KEY && ev.value == 1 &&
                           (ev.code == KEY_VOLUMEUP_CODE || ev.code == KEY_VOLUMEDOWN_CODE)) {
                    if (ev.code == KEY_VOLUMEUP_CODE) {
                        adjust_station_volume(1);
                    } else {
                        adjust_station_volume(-1);
                    }
                }
            }
        }

        if (interrupted) {
            break;
        }
    }

    if (!interrupted) {
        return 0;
    }

    /*
     * Кнопка была нажата и процесс мгновенно остановлен.
     * Проверим: это короткое нажатие (просто заткнуться) или зажатие >= 350 мс (новый вопрос)?
     */
    while (keep_running && *btn_is_down) {
        drain_vqe_to_prebuf(vqe_fd);
        if ((now_ms() - press_ms) >= LONG_PRESS_MS) {
            return -2; /* Переход сразу в новое слушание! */
        }
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(touch_fd, &rfds);
        FD_SET(vqe_fd, &rfds);
        int maxfd = (touch_fd > vqe_fd) ? touch_fd : vqe_fd;
        struct timeval tv = {0, 20000};
        if (select(maxfd + 1, &rfds, NULL, NULL, &tv) > 0 && FD_ISSET(touch_fd, &rfds)) {
            struct input_event ev;
            while (read(touch_fd, &ev, sizeof(ev)) == sizeof(ev)) {
                if (ev.type == EV_KEY && ev.code == KEY_VOICECOMMAND_CODE && ev.value == 0) {
                    *btn_is_down = 0;
                    break;
                }
            }
        }
    }

    return -1;
}

int main(void) {
    signal(SIGINT, handle_sig);
    signal(SIGTERM, handle_sig);

    const char *server_url = getenv("ASSISTANT_URL");
    if (!server_url || !server_url[0]) {
        server_url = "http://192.168.2.85:8730";
    }
    snprintf(g_server_url, sizeof(g_server_url), "%s", server_url);
    const char *vqe_sock_path = getenv("MICTAP_SOCKET");
    if (!vqe_sock_path || !vqe_sock_path[0]) {
        vqe_sock_path = "/tmp/vqe.sock";
    }
    const char *spot_sock_path = getenv("SPOTTER_SOCKET");
    if (!spot_sock_path || !spot_sock_path[0]) {
        spot_sock_path = "/tmp/spotter.sock";
    }
    const char *event_dev = getenv("TOUCH_EVENT_DEV");
    if (!event_dev || !event_dev[0]) {
        event_dev = "/dev/input/event0";
    }

    unlink("/data/quasar/data/button_click_prohibited");
    sync_hardware_volume();
    clear_ring_leds();

    int touch_fd = open(event_dev, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (touch_fd < 0) {
        perror("open event_dev");
        return 1;
    }

    /* Открываем сокет VQE постоянно для пред-буфера */
    unlink(vqe_sock_path);
    int vqe_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | O_CLOEXEC, 0);
    if (vqe_fd < 0) {
        perror("socket vqe");
        return 1;
    }
    int rcvbuf = 262144;
    setsockopt(vqe_fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    struct sockaddr_un vaddr;
    memset(&vaddr, 0, sizeof(vaddr));
    vaddr.sun_family = AF_UNIX;
    strncpy(vaddr.sun_path, vqe_sock_path, sizeof(vaddr.sun_path) - 1);
    if (bind(vqe_fd, (struct sockaddr *)&vaddr, sizeof(vaddr)) < 0) {
        perror("bind vqe");
        return 1;
    }
    chmod(vqe_sock_path, 0666);

    /* Открываем сокет событий споттера ("WAKE" / "STOP") */
    unlink(spot_sock_path);
    int spot_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | O_CLOEXEC, 0);
    if (spot_fd < 0) {
        perror("socket spotter");
        return 1;
    }
    struct sockaddr_un saddr;
    memset(&saddr, 0, sizeof(saddr));
    saddr.sun_family = AF_UNIX;
    strncpy(saddr.sun_path, spot_sock_path, sizeof(saddr.sun_path) - 1);
    if (bind(spot_fd, (struct sockaddr *)&saddr, sizeof(saddr)) < 0) {
        perror("bind spotter");
        return 1;
    }
    chmod(spot_sock_path, 0666);

    uint8_t *pcm_buf = malloc(MAX_PCM_BYTES);
    if (!pcm_buf) {
        perror("malloc pcm_buf");
        return 1;
    }

    fprintf(stderr,
            "[touch-assistant] Запущен (режим Qwen вместо Алисы): server=%s\n"
            "  - Слово «Алиса» -> вопрос в Qwen\n"
            "  - Зажатие кнопки (>=%d мс) -> вопрос в Qwen\n"
            "  - Короткое нажатие кнопки -> заткнуться / отмена\n",
            server_url, LONG_PRESS_MS);

    int btn_is_down = 0;
    uint64_t btn_down_ms = 0;
    int pending_trigger = 0;

    while (keep_running) {
        reap_async_audio();
        drain_vqe_to_prebuf(vqe_fd);

        int trigger = pending_trigger;
        pending_trigger = 0;

        if (!trigger) {
            if (btn_is_down && (now_ms() - btn_down_ms) >= LONG_PRESS_MS) {
                trigger = TRIGGER_HOLD;
            }
        }

        if (!trigger) {
            fd_set rfds;
            FD_ZERO(&rfds);
            FD_SET(touch_fd, &rfds);
            FD_SET(spot_fd, &rfds);
            FD_SET(vqe_fd, &rfds);
            int maxfd = touch_fd;
            if (spot_fd > maxfd) maxfd = spot_fd;
            if (vqe_fd > maxfd) maxfd = vqe_fd;

            struct timeval tv;
            tv.tv_sec = 0;
            tv.tv_usec = btn_is_down ? 20000 : 250000;

            int r = select(maxfd + 1, &rfds, NULL, NULL, &tv);
            if (r < 0) {
                if (errno == EINTR) continue;
                break;
            }

            if (r > 0 && FD_ISSET(vqe_fd, &rfds)) {
                drain_vqe_to_prebuf(vqe_fd);
            }

            if (r > 0 && FD_ISSET(spot_fd, &rfds)) {
                int wake = 0, stop = 0, play = 0, next = 0, volup = 0, voldown = 0;
                drain_spotter_sock(spot_fd, &wake, &stop, &play, &next, &volup, &voldown);
                if (volup) {
                    fprintf(stderr, "[touch-assistant] Прямая команда: «громче»\n");
                    adjust_station_volume(1);
                } else if (voldown) {
                    fprintf(stderr, "[touch-assistant] Прямая команда: «тише»\n");
                    adjust_station_volume(-1);
                } else if (wake) {
                    trigger = TRIGGER_WAKEWORD;
                } else if (play) {
                    fprintf(stderr, "[touch-assistant] Прямая команда «играй» -> запуск Apple Music\n");
                    play_wav_async("/data/mictap/chime.wav");
                    char play_cmd[256];
                    snprintf(play_cmd, sizeof(play_cmd), "/opt/bin/curl -sS -m 5 -X POST %s/music/play >/dev/null 2>&1 &", server_url);
                    system(play_cmd);
                } else if (next) {
                    fprintf(stderr, "[touch-assistant] Прямая команда «следующий» -> переключение Apple Music\n");
                    play_wav_async("/data/mictap/vol_click.wav");
                    char next_cmd[256];
                    snprintf(next_cmd, sizeof(next_cmd), "/opt/bin/curl -sS -m 5 -X POST %s/music/next >/dev/null 2>&1 &", server_url);
                    system(next_cmd);
                } else if (stop) {
                    fprintf(stderr, "[touch-assistant] Прямая команда «стоп» -> остановка таймеров и музыки\n");
                    char stop_cmd[256];
                    snprintf(stop_cmd, sizeof(stop_cmd),
                             "for p in $(ps -o pid,args 2>/dev/null | awk '/aplay/ && !/awk/ {print $1}'); do kill -15 \"$p\" 2>/dev/null; done; "
                             "/data/mictap/timer-clock.sh stop >/dev/null 2>&1; "
                             "/opt/bin/curl -sS -m 3 -X POST %s/music/pause >/dev/null 2>&1 &", server_url);
                    system(stop_cmd);
                    stop_async_audio();
                    stop_led();
                }
            }

            if (r > 0 && FD_ISSET(touch_fd, &rfds)) {
                struct input_event ev;
                while (read(touch_fd, &ev, sizeof(ev)) == sizeof(ev)) {
                    if (ev.type == EV_KEY && ev.code == KEY_VOICECOMMAND_CODE) {
                        if (ev.value == 1) {
                            btn_is_down = 1;
                            btn_down_ms = now_ms();
                        } else if (ev.value == 0) {
                            if (btn_is_down && (now_ms() - btn_down_ms) < LONG_PRESS_MS) {
                                fprintf(stderr, "[touch-assistant] Короткое нажатие в покое.\n");
                                stop_async_audio();
                                stop_led();
                            }
                            btn_is_down = 0;
                        }
                    } else if (ev.type == EV_KEY && ev.value == 1 &&
                               (ev.code == KEY_VOLUMEUP_CODE || ev.code == KEY_VOLUMEDOWN_CODE)) {
                        if (ev.code == KEY_VOLUMEUP_CODE) {
                            adjust_station_volume(1);
                        } else {
                            adjust_station_volume(-1);
                        }
                    }
                }
            }

            if (!trigger && btn_is_down && (now_ms() - btn_down_ms) >= LONG_PRESS_MS) {
                trigger = TRIGGER_HOLD;
            }
        }

        if (!trigger) {
            continue;
        }

        /* === СТАРТ ДИАЛОГА С QWEN === */
        sync_hardware_volume();
        int cur_vol = get_spk_volume();
        fprintf(stderr, "[touch-assistant] Активация (%s)! SPK=%d, слушаю...\n",
                (trigger == TRIGGER_WAKEWORD) ? "голос «Алиса»" : "зажатие кнопки",
                cur_vol);

        start_led("/system/vendor/quasar/ledpatterns/oknix/alice_listening.led");
        stop_async_audio();
        /* Глушим любые фоновые сигналы таймера при активации */
        system("for p in $(ps -o pid,args 2>/dev/null | awk '/aplay.*timer/ && !/awk/ {print $1}'); do kill -15 \"$p\" 2>/dev/null; done; /data/mictap/timer-clock.sh stop >/dev/null 2>&1");
        play_wav_async("/data/mictap/chime.wav");

        ssize_t recorded = record_dialog(touch_fd, vqe_fd, spot_fd,
                                         pcm_buf, MAX_PCM_BYTES,
                                         trigger, &btn_is_down);
        stop_async_audio();

        if (recorded == -2) {
            /* Во время слушания распознали «громче»/«тише» — команда уже выполнена */
            stop_led();
            drain_spotter_sock(spot_fd, NULL, NULL, NULL, NULL, NULL, NULL);
            continue;
        }

        if (recorded < 0) {
            /* Отменено пользователем (нажатие кнопки или «Стоп») */
            stop_led();
            play_wav_async("/data/mictap/cancel.wav");
            drain_spotter_sock(spot_fd, NULL, NULL, NULL, NULL, NULL, NULL);
            continue;
        }

        fprintf(stderr, "[touch-assistant] Записано %zd байт (%.2f с)\n",
                recorded, (double)recorded / (double)BYTES_PER_SEC);

        if (recorded < 6400) {
            stop_led();
            continue;
        }

        const char *in_wav = "/tmp/assistant_in.wav";
        const char *out_wav = "/tmp/assistant_out.wav";
        const char *scaled_wav = "/tmp/assistant_out_scaled.wav";
        unlink(out_wav);
        unlink(scaled_wav);

        if (write_wav_file(in_wav, pcm_buf, (uint32_t)recorded) < 0) {
            stop_led();
            continue;
        }

        /* Индикация размышления + прерываемый запрос к Mac */
        start_led("/system/vendor/quasar/ledpatterns/oknix/alice_speaking.led");
        char url[256];
        snprintf(url, sizeof(url), "%s/voice/reply", server_url);
        fprintf(stderr, "[touch-assistant] Запрос в Qwen (%s)...\n", url);

        pid_t curl_pid = fork();
        if (curl_pid == 0) {
            int devnull = open("/dev/null", O_RDWR);
            if (devnull >= 0) {
                dup2(devnull, STDOUT_FILENO);
                dup2(devnull, STDERR_FILENO);
                if (devnull > 2) close(devnull);
            }
            execl("/opt/bin/curl", "curl", "-sS", "-f", "--max-time", "25",
                  "-X", "POST", "-H", "Content-Type: audio/wav",
                  "--data-binary", "@/tmp/assistant_in.wav",
                  "-o", out_wav, url, (char *)NULL);
            _exit(127);
        }

        int curl_res = run_interruptible_pid(curl_pid, touch_fd, vqe_fd, spot_fd, &btn_is_down);
        if (curl_res == -2) {
            stop_led();
            pending_trigger = TRIGGER_HOLD;
            continue;
        } else if (curl_res == -1) {
            stop_led();
            play_wav_async("/data/mictap/cancel.wav");
            drain_spotter_sock(spot_fd, NULL, NULL, NULL, NULL, NULL, NULL);
            continue;
        }

        struct stat st;
        if (stat(out_wav, &st) == 0 && st.st_size > 100) {
            int vol = get_spk_volume();
            fprintf(stderr, "[touch-assistant] Ответ Qwen (%ld байт), громкость=%d...\n",
                    (long)st.st_size, vol);
            const char *play_target = out_wav;
            sync_hardware_volume();
            ensure_alsa_free();

            if (vol > 0) {
                pid_t aplay_pid = fork();
                if (aplay_pid == 0) {
                    int devnull = open("/dev/null", O_RDWR);
                    if (devnull >= 0) {
                        dup2(devnull, STDOUT_FILENO);
                        dup2(devnull, STDERR_FILENO);
                        if (devnull > 2) close(devnull);
                    }
                    execl("/usr/bin/aplay", "aplay", "-q", "-D", "dmix_spk_plug", play_target, (char *)NULL);
                    _exit(127);
                }

                int play_res = run_interruptible_pid(aplay_pid, touch_fd, vqe_fd, spot_fd, &btn_is_down);
                if (play_res == -2) {
                    stop_led();
                    pending_trigger = TRIGGER_HOLD;
                    continue;
                }
            }
            fprintf(stderr, "[touch-assistant] Диалог завершён.\n");
        } else {
            /* Если сервер вернул 204 (пустая фраза / только слово Алиса) — тихо гасим кольцо */
            fprintf(stderr, "[touch-assistant] Пустая фраза или тишина — ответ не требуется.\n");
        }

        stop_led();
        /* Очищаем возможные ложные срабатывания споттера во время собственной речи */
        drain_spotter_sock(spot_fd, NULL, NULL, NULL, NULL, NULL, NULL);
    }

    stop_async_audio();
    stop_led();
    free(pcm_buf);
    close(spot_fd);
    close(vqe_fd);
    close(touch_fd);
    unlink(spot_sock_path);
    unlink(vqe_sock_path);
    return 0;
}
