/*
 * mictap.so — LD_PRELOAD-шим для maind --platformd:
 * 1. Перехватывает YandexVqeC_GetOutputChannelData из libyandex_evil.so:
 *    копирует канал vqe_0 (kind=6, index=0, моно 16 кГц float32 после AEC)
 *    в S16_LE PCM и отправляет в неблокирующий Unix-сокет /tmp/vqe.sock.
 * 2. Перехватывает spotter_get_next_event и spotter_get_next_event_seamless:
 *    ловит локальный NPU-споттер слова «Алиса» (и команд «стоп»/«хватит»),
 *    уведомляет наш демон через /tmp/spotter.sock ("WAKE" / "STOP") и
 *    возвращает 0 (NEED_MORE_DATA) в platformd, чтобы облачная Алиса молчала.
 * 3. Перехватывает read() для фильтрации центральной сенсорной кнопки
 *    (EV_KEY=1, code=582) от штатной Алисы, оставляя физические кнопки
 *    громкости (114/115) и экранную индикацию полностью рабочими.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

static void *(*real_GetOutputChannelData)(void *engine, unsigned int kind,
                                          unsigned int index, const float **data,
                                          size_t *frames) = NULL;
static int (*real_spotter_get_next_event)(void *spotter, void **err) = NULL;
static int (*real_spotter_get_next_event_seamless)(void *spotter, void **err) = NULL;
static const char *(*real_spotter_get_current_phrase)(void *spotter, void **err) = NULL;
static ssize_t (*real_read)(int fd, void *buf, size_t count) = NULL;

static struct sockaddr_un vqe_addr;
static socklen_t vqe_addr_len = 0;
static struct sockaddr_un spot_addr;
static socklen_t spot_addr_len = 0;
static int sock_fd = -1;
static int initialized = 0;
static uint64_t last_wake_ms = 0;
static uint64_t last_stop_ms = 0;
static uint64_t last_volup_ms = 0;
static uint64_t last_voldown_ms = 0;

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000ULL);
}

static void log_msg(const char *s) {
    int fd = open("/tmp/mictap.log", O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd >= 0) {
        write(fd, s, strlen(s));
        close(fd);
    }
}

static void init_tap(void) {
    initialized = 1;
    const char *path = getenv("MICTAP_SOCKET");
    if (!path || !path[0]) {
        path = "/tmp/vqe.sock";
    }

    memset(&vqe_addr, 0, sizeof(vqe_addr));
    vqe_addr.sun_family = AF_UNIX;
    strncpy(vqe_addr.sun_path, path, sizeof(vqe_addr.sun_path) - 1);
    vqe_addr_len = sizeof(vqe_addr.sun_family) + strlen(vqe_addr.sun_path);

    const char *spath = getenv("SPOTTER_SOCKET");
    if (!spath || !spath[0]) {
        spath = "/tmp/spotter.sock";
    }
    memset(&spot_addr, 0, sizeof(spot_addr));
    spot_addr.sun_family = AF_UNIX;
    strncpy(spot_addr.sun_path, spath, sizeof(spot_addr.sun_path) - 1);
    spot_addr_len = sizeof(spot_addr.sun_family) + strlen(spot_addr.sun_path);

    sock_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (sock_fd >= 0) {
        log_msg("[mictap] initialized vqe & spotter sockets\n");
    }
}

static void notify_spotter(const char *cmd) {
    if (!initialized) {
        init_tap();
    }
    if (sock_fd >= 0) {
        sendto(sock_fd, cmd, strlen(cmd),
               MSG_DONTWAIT | MSG_NOSIGNAL,
               (struct sockaddr *)&spot_addr, spot_addr_len);
    }
}

static void handle_spotter_hit(void *spotter, int ev, const char *src) {
    if (!real_spotter_get_current_phrase) {
        real_spotter_get_current_phrase = dlsym(RTLD_NEXT, "spotter_get_current_phrase");
    }
    void *dummy_err = NULL;
    const char *phrase = NULL;
    if (real_spotter_get_current_phrase && spotter) {
        phrase = real_spotter_get_current_phrase(spotter, &dummy_err);
    }
    if (!phrase) {
        phrase = "(null)";
    }

    /* Игнорируем служебные события VAD (<speech_started>, <speech_finished>) */
    if (phrase[0] == '<') {
        return;
    }

    char buf[256];
    snprintf(buf, sizeof(buf), "[mictap] %s ev=%d phrase='%s'\n", src, ev, phrase);
    log_msg(buf);

    /* В spotter_get_next_event ev=1 — это предварительный subhit (порог 0.435),
     * а 2/3/5/8 — подтверждённое срабатывание (порог 0.691).
     * В spotter_get_next_event_seamless срабатывание — это ev=1 или ev=2. */
    if (strcmp(src, "spotter_get_next_event") == 0 && ev == 1) {
        return;
    }

    uint64_t t = now_ms();
    if (strstr(phrase, "алис") || strstr(phrase, "Алис") ||
        strstr(phrase, "alisa") || strstr(phrase, "яндекс") ||
        strstr(phrase, "yandex")) {
        if (t - last_wake_ms >= 1200) {
            last_wake_ms = t;
            notify_spotter("WAKE");
            log_msg("[mictap] -> sent WAKE to touch-assistant\n");
        }
    } else if (strstr(phrase, "стоп") || strstr(phrase, "хватит") ||
               strstr(phrase, "пауза")) {
        if (t - last_stop_ms >= 800) {
            last_stop_ms = t;
            notify_spotter("STOP");
            log_msg("[mictap] -> sent STOP to touch-assistant\n");
        }
    } else if (strstr(phrase, "громче") || strstr(phrase, "выше")) {
        if (t - last_volup_ms >= 500) {
            last_volup_ms = t;
            notify_spotter("VOLUP");
            log_msg("[mictap] -> sent VOLUP to touch-assistant\n");
        }
    } else if (strstr(phrase, "тише") || strstr(phrase, "ниже")) {
        if (t - last_voldown_ms >= 500) {
            last_voldown_ms = t;
            notify_spotter("VOLDOWN");
            log_msg("[mictap] -> sent VOLDOWN to touch-assistant\n");
        }
    }
}

void *YandexVqeC_GetOutputChannelData(void *engine, unsigned int kind,
                                     unsigned int index, const float **data,
                                     size_t *frames) {
    int orig_errno = errno;
    if (!real_GetOutputChannelData) {
        real_GetOutputChannelData = dlsym(RTLD_NEXT, "YandexVqeC_GetOutputChannelData");
    }
    errno = orig_errno;

    if (!real_GetOutputChannelData) {
        _exit(2);
    }

    void *ret = real_GetOutputChannelData(engine, kind, index, data, frames);
    int call_errno = errno;

    /* kind 6 = VQE, index 0 = vqe_0 (обработанный 16 кГц моно поток) */
    if (!ret && kind == 6 && index == 0 && data && frames && *data && *frames > 0 && *frames <= 4096) {
        if (!initialized) {
            init_tap();
        }

        if (sock_fd >= 0) {
            int16_t packet[160]; /* до 10 мс звука на датаграмму */
            size_t total = *frames;
            for (size_t offset = 0; offset < total;) {
                size_t count = total - offset;
                if (count > 160) count = 160;

                for (size_t i = 0; i < count; ++i) {
                    float s = (*data)[offset + i] * 16.0f; /* +24 dB до квантования в int16 */
                    if (s != s) {
                        packet[i] = 0; /* NaN */
                    } else if (s >= 1.0f) {
                        packet[i] = 32767;
                    } else if (s <= -1.0f) {
                        packet[i] = -32768;
                    } else {
                        packet[i] = (int16_t)(s * 32767.0f);
                    }
                }

                sendto(sock_fd, packet, count * sizeof(int16_t),
                       MSG_DONTWAIT | MSG_NOSIGNAL,
                       (struct sockaddr *)&vqe_addr, vqe_addr_len);
                offset += count;
            }
        }
    }

    errno = call_errno;
    return ret;
}

int spotter_get_next_event(void *spotter, void **err) {
    int orig_errno = errno;
    if (!real_spotter_get_next_event) {
        real_spotter_get_next_event = dlsym(RTLD_NEXT, "spotter_get_next_event");
    }
    errno = orig_errno;
    if (!real_spotter_get_next_event) {
        _exit(2);
    }

    int ev = 0;
    for (int iter = 0; iter < 8; ++iter) {
        ev = real_spotter_get_next_event(spotter, err);
        /* 0=NEED_1ST, 4=NEED_2ND, 7=FINISH, 9=NEED_SEAM_VAD */
        if (ev == 0 || ev == 4 || ev == 7 || ev == 9) {
            break;
        }
        /* 1=SUBHIT, 2=CONFIRM_1ST, 3=HIT_1ST, 5=CONFIRM_2ND, 8=ACTIVATION_SEAMLESS */
        if (ev == 1 || ev == 2 || ev == 3 || ev == 5 || ev == 8) {
            handle_spotter_hit(spotter, ev, "spotter_get_next_event");
        }
    }

    /* Всегда возвращаем 0 (NEED_MORE_DATA), чтобы штатная Алиса не активировалась */
    return 0;
}

int spotter_get_next_event_seamless(void *spotter, void **err) {
    int orig_errno = errno;
    if (!real_spotter_get_next_event_seamless) {
        real_spotter_get_next_event_seamless = dlsym(RTLD_NEXT, "spotter_get_next_event_seamless");
    }
    errno = orig_errno;
    if (!real_spotter_get_next_event_seamless) {
        _exit(2);
    }

    int ev = 0;
    for (int iter = 0; iter < 8; ++iter) {
        ev = real_spotter_get_next_event_seamless(spotter, err);
        /* В seamless-режиме 0 и 3 означают отсутствие срабатывания */
        if (ev == 0 || ev == 3) {
            break;
        }
        handle_spotter_hit(spotter, ev, "spotter_get_next_event_seamless");
    }

    return ev;
}

ssize_t read(int fd, void *buf, size_t count) {
    int orig_errno = errno;
    if (!real_read) {
        real_read = dlsym(RTLD_NEXT, "read");
    }
    errno = orig_errno;
    if (!real_read) {
        _exit(2);
    }

    ssize_t n = real_read(fd, buf, count);
    int call_errno = errno;

    if (n >= 16 && (n % 16 == 0) && n <= 256 && buf) {
        struct {
            uint32_t tv_sec;
            uint32_t tv_usec;
            uint16_t type;
            uint16_t code;
            int32_t  value;
        } *evs = buf;
        size_t num = (size_t)n / 16;
        for (size_t i = 0; i < num; ++i) {
            if (evs[i].type == 1 && evs[i].code == 582) {
                evs[i].type = 0;
                evs[i].code = 0;
                evs[i].value = 0;
            }
        }
    }

    errno = call_errno;
    return n;
}
