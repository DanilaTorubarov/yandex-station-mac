"""Управление Apple Music на Mac и вывод звука через AirPlay на Яндекс Станцию Мини 2."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

STATION_AIRPLAY_NAME = "Яндекс Станция Мини 2"

# Персональная радиостанция в Apple Music (Danila Torubarov's Station / «Моя волна»)
PERSONAL_STATION_URL = (
    "music://music.apple.com/kz/station/"
    "%D0%B4%D0%B0%D0%BD%D0%B8%D0%BB%D0%B0-%D1%82%D0%BE%D1%80%D1%83%D0%B1%D0%B0%D1%80%D0%BE%D0%B2s-station/"
    "ra.u-27df3e6f021277901abb70ff757aa95f"
)
DISCOVERY_STATION_URL = (
    "music://music.apple.com/kz/station/discovery-station/"
    "ra.q-GAI6IDI3ZGYzZTZmMDIxMjc3OTAxYWJiNzBmZjc1N2FhOTVm"
)

# Официальные жанровые радиостанции Apple Music
ROCK_STATION_URL = "music://music.apple.com/station/ra.985484166"
LOFI_STATION_URL = "music://music.apple.com/station/ra.1461159807"
JAZZ_STATION_URL = "music://music.apple.com/station/ra.985484145"

_GENRE_STATIONS: dict[str, tuple[str, str]] = {
    "рок": (ROCK_STATION_URL, "рок-радио"),
    "rock": (ROCK_STATION_URL, "рок-радио"),
    "лоуфай": (LOFI_STATION_URL, "лоуфай для работы"),
    "лоу-фай": (LOFI_STATION_URL, "лоуфай для работы"),
    "лоу фай": (LOFI_STATION_URL, "лоуфай для работы"),
    "lofi": (LOFI_STATION_URL, "лоуфай для работы"),
    "lo-fi": (LOFI_STATION_URL, "лоуфай для работы"),
    "для работы": (LOFI_STATION_URL, "лоуфай для работы"),
    "фон": (LOFI_STATION_URL, "фоновую музыку"),
    "фоновую": (LOFI_STATION_URL, "фоновую музыку"),
    "чилл": (LOFI_STATION_URL, "чилл-радио"),
    "chill": (LOFI_STATION_URL, "чилл-радио"),
    "джаз": (JAZZ_STATION_URL, "джаз"),
    "jazz": (JAZZ_STATION_URL, "джаз"),
}

_PLAYLIST_ALIASES: dict[str, str] = {
    "киберпанк": "Cyberpunk",
    "cyberpunk": "Cyberpunk",
    "хотлайн": "Hotline Miami OST",
    "майами": "Hotline Miami OST",
    "hotline": "Hotline Miami OST",
    "попса": "Попса ипучая",
    "попсу": "Попса ипучая",
    "любимые": "Favourite Songs",
    "избранные": "Favourite Songs",
    "любимое": "Favourite Songs",
    "открытий": "__DISCOVERY_STATION__",
    "дискавери": "__DISCOVERY_STATION__",
}


def _build_jxa_play(target_playlist: str = "", resume_only: bool = False, target_device: str = "") -> str:
    pl_json = json.dumps(target_playlist)
    resume_js = "true" if resume_only else "false"
    device_json = json.dumps(target_device)
    return f"""
(function() {{
    var music = Application("Music");
    if (!music.running()) {{
        music.launch();
        delay(0.8);
    }}

    // 1. Управление аудиоустройствами AirPlay (Яндекс Станция / Наушники / Компьютер)
    var reqDevice = {device_json};
    var stationName = "{STATION_AIRPLAY_NAME}";
    try {{
        var devs = music.airplayDevices();
        var hasBT = false;
        for (var i = 0; i < devs.length; i++) {{
            if (devs[i].kind() === "Bluetooth device") {{
                hasBT = true;
                break;
            }}
        }}

        // Если явно запрошена колонка, ИЛИ если наушники сняты/не подключены (и не запрошены наушники):
        var wantStation = (reqDevice === "station") || (!hasBT && reqDevice !== "headphones");

        for (var i = 0; i < devs.length; i++) {{
            var d = devs[i];
            var dName = d.name();
            var dKind = d.kind();
            if (wantStation) {{
                if (dName === stationName) {{
                    d.selected = true;
                }} else if (dKind === "computer" || dKind === "Bluetooth device") {{
                    try {{ d.selected = false; }} catch (e) {{}}
                }}
            }} else if (reqDevice === "headphones") {{
                if (dKind === "Bluetooth device" || dKind === "computer") {{
                    d.selected = true;
                }} else if (dName === stationName) {{
                    try {{ d.selected = false; }} catch (e) {{}}
                }}
            }}
        }}
    }} catch (e) {{}}

    function findPlaylist(candidates) {{
        try {{
            var pls = music.playlists();
            for (var c = 0; c < candidates.length; c++) {{
                var want = candidates[c].toLowerCase();
                if (!want) continue;
                for (var i = 0; i < pls.length; i++) {{
                    var pname = pls[i].name().toLowerCase();
                    if (pname === want || pname.indexOf(want) !== -1) {{
                        return pls[i];
                    }}
                }}
            }}
        }} catch (e) {{}}
        return null;
    }}

    // 2. Запускаем воспроизведение
    var requested = {pl_json};
    var resumeOnly = {resume_js};
    var st = "stopped";
    try {{
        st = music.playerState();
    }} catch (e) {{}}

    var canResumeTrack = false;
    try {{
        if (st === "paused" || (st === "stopped" && music.currentTrack.name() != null)) {{
            canResumeTrack = true;
        }}
    }} catch (e) {{}}

    if (requested && requested.indexOf("music://") === 0) {{
        music.openLocation(requested);
        delay(1.5);
    }} else if (requested === "__DISCOVERY_STATION__") {{
        music.openLocation("{DISCOVERY_STATION_URL}");
        delay(1.8);
    }} else if (requested) {{
        var pl = findPlaylist([requested, "Favourite Songs", "Favorite Songs", "Избранные песни", "Music"]);
        if (pl) {{
            pl.play();
        }} else {{
            music.openLocation("{PERSONAL_STATION_URL}");
        }}
        delay(0.5);
    }} else if (resumeOnly && canResumeTrack) {{
        music.play();
        delay(0.4);
    }} else if (canResumeTrack && !requested) {{
        music.play();
        delay(0.4);
    }} else {{
        // По умолчанию («играй», «включи музыку», «включи мою волну») запускаем Danila Torubarov's Station
        music.openLocation("{PERSONAL_STATION_URL}");
        delay(1.8);
    }}

    try {{
        if (music.playerState() !== "playing") {{
            music.play();
            delay(0.4);
        }}
    }} catch (e) {{}}

    var trackName = "";
    var artistName = "";
    try {{
        trackName = music.currentTrack.name();
        artistName = music.currentTrack.artist();
    }} catch (e) {{}}

    return JSON.stringify({{
        ok: true,
        playerState: "playing",
        track: trackName,
        artist: artistName
    }});
}})();
"""


_JXA_PAUSE = """
(function() {
    var music = Application("Music");
    if (!music.running()) {
        return JSON.stringify({ok: true, playerState: "stopped"});
    }
    try {
        music.pause();
        return JSON.stringify({ok: true, playerState: "paused"});
    } catch (e) {
        return JSON.stringify({ok: false, error: String(e)});
    }
})();
"""

_JXA_NEXT = """
(function() {
    var music = Application("Music");
    try {
        music.nextTrack();
        delay(0.3);
        var t = "";
        var a = "";
        try {
            t = music.currentTrack.name();
            a = music.currentTrack.artist();
        } catch (e) {}
        return JSON.stringify({ok: true, track: t, artist: a});
    } catch (e) {
        return JSON.stringify({ok: false, error: String(e)});
    }
})();
"""

_APPLESCRIPT_CONTROL = """
on run argv
    set mode to item 1 of argv
    set step_sec to 20
    if (count of argv) > 1 then
        try
            set step_sec to (item 2 of argv) as integer
        end try
    end if
    
    tell application "Music"
        if not (running) then
            return "{\\"ok\\": false, \\"error\\": \\"not_running\\"}"
        end if
        if player state is stopped then
            return "{\\"ok\\": false, \\"error\\": \\"stopped\\"}"
        end if
        
        try
            set tr to current track
            set tr_cls to class of tr as string
            set tr_name to name of tr
            set tr_artist to artist of tr
        on error
            return "{\\"ok\\": false, \\"error\\": \\"no_track\\"}"
        end try
        
        set is_radio to (tr_cls is "URL track")
        
        if mode is "start" then
            set player position to 0
            return "{\\"ok\\": true, \\"action\\": \\"restart\\", \\"is_radio\\": " & is_radio & ", \\"track\\": \\"" & tr_name & "\\", \\"artist\\": \\"" & tr_artist & "\\"}"
        else if mode is "rewind" then
            set p to player position
            set new_p to p - step_sec
            if new_p < 0 then set new_p to 0
            set player position to new_p
            return "{\\"ok\\": true, \\"action\\": \\"rewind\\", \\"is_radio\\": " & is_radio & ", \\"seconds\\": " & step_sec & ", \\"pos\\": " & (round new_p) & ", \\"track\\": \\"" & tr_name & "\\", \\"artist\\": \\"" & tr_artist & "\\"}"
        else if mode is "forward" then
            set p to player position
            set new_p to p + step_sec
            set player position to new_p
            return "{\\"ok\\": true, \\"action\\": \\"forward\\", \\"is_radio\\": " & is_radio & ", \\"seconds\\": " & step_sec & ", \\"pos\\": " & (round new_p) & ", \\"track\\": \\"" & tr_name & "\\", \\"artist\\": \\"" & tr_artist & "\\"}"
        else
            -- previous
            if is_radio then
                set player position to 0
                return "{\\"ok\\": true, \\"action\\": \\"radio_restart\\", \\"is_radio\\": true, \\"track\\": \\"" & tr_name & "\\", \\"artist\\": \\"" & tr_artist & "\\"}"
            else
                set old_id to id of tr
                previous track
                delay 0.3
                try
                    set new_tr to current track
                    set new_name to name of new_tr
                    set new_artist to artist of new_tr
                    if id of new_tr is old_id then
                        return "{\\"ok\\": true, \\"action\\": \\"restart\\", \\"is_radio\\": false, \\"track\\": \\"" & new_name & "\\", \\"artist\\": \\"" & new_artist & "\\"}"
                    else
                        return "{\\"ok\\": true, \\"action\\": \\"previous\\", \\"is_radio\\": false, \\"track\\": \\"" & new_name & "\\", \\"artist\\": \\"" & new_artist & "\\"}"
                    end if
                on error
                    return "{\\"ok\\": true, \\"action\\": \\"restart\\", \\"is_radio\\": false, \\"track\\": \\"" & tr_name & "\\", \\"artist\\": \\"" & tr_artist & "\\"}"
                end try
            end if
        end if
    end tell
end run
"""

_JXA_CURRENT = """
(function() {
    var music = Application("Music");
    if (!music.running()) {
        return JSON.stringify({ok: false, playing: false});
    }
    try {
        var st = music.playerState();
        var t = music.currentTrack.name();
        var a = music.currentTrack.artist();
        return JSON.stringify({ok: true, playing: st === "playing", track: t, artist: a});
    } catch (e) {
        return JSON.stringify({ok: false, playing: false, error: String(e)});
    }
})();
"""


def _run_jxa(script: str, timeout: float = 8.0) -> dict[str, Any]:
    """Выполняет JavaScript for Automation (JXA) скрипт в macOS."""
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
        log.warning("JXA вернул код %d: %s", res.returncode, res.stderr)
        return {"ok": False, "error": res.stderr}
    except Exception as exc:
        log.warning("Ошибка выполнения JXA: %s", exc)
        return {"ok": False, "error": str(exc)}


def _run_applescript_control(mode: str, step_sec: int = 20, timeout: float = 6.0) -> dict[str, Any]:
    """Выполняет AppleScript для точного управления позицией и треками в Music.app."""
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", _APPLESCRIPT_CONTROL, mode, str(step_sec)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
        log.warning("AppleScript control вернул код %d: %s", res.returncode, res.stderr)
        return {"ok": False, "error": res.stderr}
    except Exception as exc:
        log.warning("Ошибка выполнения AppleScript control: %s", exc)
        return {"ok": False, "error": str(exc)}


def play_apple_music(playlist: str = "", resume_only: bool = False, target_device: str = "") -> dict[str, Any]:
    """Открывает Apple Music, выбирает вывод (Яндекс Станция / наушники) и запускает воспроизведение."""
    return _run_jxa(_build_jxa_play(playlist, resume_only=resume_only, target_device=target_device), timeout=10.0)


def switch_apple_music_output(target_device: str = "station") -> dict[str, Any]:
    """Переключает вывод воспроизведения Apple Music между колонкой и наушниками."""
    return play_apple_music(resume_only=True, target_device=target_device)


def pause_apple_music() -> dict[str, Any]:
    """Ставит воспроизведение в Apple Music на паузу."""
    return _run_jxa(_JXA_PAUSE)


def next_apple_music() -> dict[str, Any]:
    """Переключает на следующий трек в Apple Music."""
    return _run_jxa(_JXA_NEXT)


def prev_apple_music() -> dict[str, Any]:
    """Переключает на предыдущий трек (в плейлисте) или перезапускает трек с начала (на радио)."""
    return _run_applescript_control("previous")


def rewind_apple_music(seconds: int = 20) -> dict[str, Any]:
    """Перематывает текущий трек назад на N секунд."""
    return _run_applescript_control("rewind", step_sec=seconds)


def forward_apple_music(seconds: int = 20) -> dict[str, Any]:
    """Перематывает текущий трек вперёд на N секунд."""
    return _run_applescript_control("forward", step_sec=seconds)


def restart_track_apple_music() -> dict[str, Any]:
    """Перематывает текущий трек в самое начало (0:00)."""
    return _run_applescript_control("start")


def current_track_apple_music() -> dict[str, Any]:
    """Возвращает текущий трек и исполнителя в Apple Music."""
    return _run_jxa(_JXA_CURRENT)


def set_apple_music_airplay_volume(level_1_to_10: int) -> dict[str, Any]:
    """Устанавливает громкость AirPlay для Яндекс Станции в Music.app (0-100%)."""
    vol = max(0, min(100, int(level_1_to_10 * 10)))
    script = f"""
    (function() {{
        var music = Application("Music");
        if (!music.running()) {{
            return JSON.stringify({{ok: false, reason: "Music not running"}});
        }}
        try {{
            music.soundVolume = {vol};
            var devs = music.airplayDevices.whose({{name: "{STATION_AIRPLAY_NAME}"}});
            if (devs.length > 0 && devs[0].selected()) {{
                devs[0].soundVolume = {vol};
            }}
            return JSON.stringify({{ok: true, volume: {vol}}});
        }} catch (e) {{
            return JSON.stringify({{ok: false, error: String(e)}});
        }}
    }})();
    """
    return _run_jxa(script)


def favorite_current_track_apple_music() -> dict[str, Any]:
    """Добавляет текущий воспроизводимый трек в любимые (Favorites) и в медиатеку Apple Music."""
    script = """
    tell application "Music"
        if not (running) or player state is stopped then
            return "{\\"ok\\": false, \\"error\\": \\"stopped\\"}"
        end if
        try
            set tr to current track
            try
                duplicate tr to source 1
            end try
            try
                set favorited of tr to true
            on error
                try
                    set loved of tr to true
                end try
            end try
            set tName to name of tr
            set tArtist to artist of tr
            return "{\\"ok\\": true, \\"track\\": \\"" & tName & "\\", \\"artist\\": \\"" & tArtist & "\\"}"
        on error errMsg
            return "{\\"ok\\": false, \\"error\\": \\"" & errMsg & "\\"}"
        end try
    end tell
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
        return {"ok": False, "error": res.stderr}
    except Exception as exc:
        log.warning("Ошибка при добавлении трека в любимые: %s", exc)
        return {"ok": False, "error": str(exc)}


def dislike_current_track_apple_music() -> dict[str, Any]:
    """Ставит дизлайк текущему треку и переключает на следующий."""
    script = """
    tell application "Music"
        if not (running) or player state is stopped then
            return "{\\"ok\\": false, \\"error\\": \\"stopped\\"}"
        end if
        try
            set tr to current track
            try
                set disliked of tr to true
            end try
            next track
            return "{\\"ok\\": true}"
        on error errMsg
            return "{\\"ok\\": false, \\"error\\": \\"" & errMsg & "\\"}"
        end try
    end tell
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
        return {"ok": False, "error": res.stderr}
    except Exception as exc:
        log.warning("Ошибка при дизлайке трека: %s", exc)
        return {"ok": False, "error": str(exc)}


# Шаблоны голосовых команд управления музыкой
_MUSIC_PLAY_RE = re.compile(
    r"\b(?:включи|запусти|поставь|вруби|сыграй|играй|продолжи|возобнови|сними\s+с\s+паузы)\b.*"
    r"\b(?:музык\w*|мо[юя]\s+волн\w*|волн\w*|мо[юя]\s+станци\w*|станци\w*|station|песн\w*|трек\w*|плейлист\w*|радио|"
    r"apple\s*music|эпл\s*мьюзик|эпл\s*мюзик|киберпанк|cyberpunk|хотлайн|hotline|попс\w*|рок|рока|rock|"
    r"лоуфай|лоу-фай|лоу\s+фай|lofi|lo-fi|джаз\w*|jazz|фонов\w+|чилл\w*|chill)\b",
    re.IGNORECASE,
)

_MUSIC_PAUSE_RE = re.compile(
    r"\b("
    r"поставь\s+(?:музык\w*|трек\w*|песн\w*|воспроизведение)?\s*на\s+паузу|"
    r"пауз[ау]\b|"
    r"на\s+паузу\b|"
    r"останови\s+(?:музык\w*|станци\w*|радио|песн\w*|трек\w*)|"
    r"(?:выключи|отключи)\s+(?:музык\w*|радио|мо[юя]\s+станци\w*|станци\w*|.*station)|"
    r"тормозни\s+(?:музык\w*|трек\w*)"
    r")\b",
    re.IGNORECASE,
)

_MUSIC_FAVORITE_RE = re.compile(
    r"\b("
    r"(?:добавь|сохрани|кинь|закинь)\s+(?:этот\s+|эту\s+)?(?:трек|песн\w*|музык\w*)?\s*в\s+(?:любим\w*|избранн\w*|плейлист\s+любим\w*)|"
    r"(?:поставь|сделай|влепи)\s+лайк\b|"
    r"лайкни\s+(?:этот\s+)?(?:трек|песн\w*)|"
    r"мне\s+нравится\s+(?:этот\s+|эта\s+)?(?:трек|песн\w*)|"
    r"(?:в\s+)?любим(?:ое|ые|ый)\s+(?:трек|песн\w*)?"
    r")\b",
    re.IGNORECASE,
)

_MUSIC_DISLIKE_RE = re.compile(
    r"\b("
    r"дизлайк\b|"
    r"поставь\s+дизлайк|"
    r"не\s+нравится\s+(?:этот\s+|эта\s+)?(?:трек|песн\w*)|"
    r"(?:больше\s+не\s+ставь|удали\s+из\s+любимых)"
    r")\b",
    re.IGNORECASE,
)

_MUSIC_NEXT_RE = re.compile(
    r"\b(?:следующ\w*\s+(?:трек|песн\w*)|переключи\s+(?:трек|песн\w*)|следующий|следующая|дальше|далее|скипни|скип)\b",
    re.IGNORECASE,
)

_MUSIC_PREV_RE = re.compile(
    r"\b(?:предыдущ\w*\s+(?:трек|песн\w*)|верни\s+(?:трек|песн\w*)|предыдущий|предыдущая|прошл\w*\s+(?:песн\w*|трек\w*))\b",
    re.IGNORECASE,
)

_MUSIC_REWIND_RE = re.compile(
    r"\b(?:перемотай\s+назад|перемотай\s+в\s+начало|с\s+начала|в\s+начало|перемотай|отмотай|назад\s+на)\b",
    re.IGNORECASE,
)

_MUSIC_FORWARD_RE = re.compile(
    r"\b(?:перемотай\s+впер[её]д|впер[её]д\s+на)\b",
    re.IGNORECASE,
)

_MUSIC_WHAT_RE = re.compile(
    r"\b(?:что\s+(?:сейчас\s+)?играет|какая\s+(?:сейчас\s+)?песня|какой\s+(?:сейчас\s+)?трек|кто\s+(?:сейчас\s+)?по[её]т)\b",
    re.IGNORECASE,
)


def _extract_target_music(t_low: str) -> tuple[str, str]:
    """Возвращает (целевой_url_или_имя, понятное_название_для_ответа)."""
    # 1. Жанровые радиостанции Apple Music (строго по границам слов, чтобы «рок» не срабатывал на «сорок» или «урок»)
    for kw, (url, label) in _GENRE_STATIONS.items():
        if re.search(rf"\b{re.escape(kw)}\b", t_low):
            return url, label

    # 2. Пользовательские плейлисты и личные станции
    for kw, target in _PLAYLIST_ALIASES.items():
        if re.search(rf"\b{re.escape(kw)}\b", t_low):
            if target == "__DISCOVERY_STATION__":
                return target, "станцию открытий"
            return target, f"плейлист {target}"

    return "", ""


def try_handle_music_command(text: str) -> str | None:
    """
    Обрабатывает голосовые команды управления музыкой:
    - «поставь рок / лоуфай для работы» / «включи плейлист Киберпанк»
    - «поставь трек на паузу» / «продолжи» / «сними с паузы»
    - «следующий трек» / «предыдущий трек»
    - «добавь этот трек в любимые» / «лайк» / «дизлайк»
    - «что сейчас играет?»
    """
    t = " ".join((text or "").split()).strip()
    if not t:
        return None
    t_low = t.lower()

    # 1. Добавление в любимые (Лайк)
    if _MUSIC_FAVORITE_RE.search(t_low):
        log.info("Команда лайк/добавить в любимые: '%s'", t)
        res = favorite_current_track_apple_music()
        if not res.get("ok"):
            if res.get("error") in ("stopped", "not_playing"):
                return "Сейчас в Apple Music ничего не играет."
            return "Не удалось добавить трек в любимые."
        tr = res.get("track", "")
        art = res.get("artist", "")
        if tr and art:
            return f"Добавила {tr} — {art} в любимые треки."
        if tr:
            return f"Добавила трек {tr} в любимые треки."
        return "Добавила текущий трек в любимые песни."

    # 2. Дизлайк
    if _MUSIC_DISLIKE_RE.search(t_low):
        log.info("Команда дизлайк: '%s'", t)
        res = dislike_current_track_apple_music()
        if not res.get("ok"):
            return "Сейчас в Apple Music ничего не играет."
        return "Поставила дизлайк и включила следующий трек."

    # 3. Пауза
    if _MUSIC_PAUSE_RE.search(t_low):
        log.info("Команда паузы музыки: '%s'", t)
        threading.Thread(target=pause_apple_music, daemon=True).start()
        return "Поставила музыку на паузу."

    # 4. Возобновление воспроизведения («продолжи», «сними с паузы», «возобнови»)
    is_resume_only = len(t_low.split()) <= 4 and bool(
        re.search(r"\b(?:продолжи|возобнови|сними\s+с\s+паузы|играй\s+дальше)\b", t_low)
    )
    if is_resume_only and not re.search(
        r"\b(?:рок|рока|лоуфай|lofi|джаз|плейлист\w*|песн\w*|киберпанк|попс\w*|рассказ\w*|истори\w*|текст)\b",
        t_low,
    ):
        log.info("Команда продолжения воспроизведения: '%s'", t)
        threading.Thread(target=play_apple_music, kwargs={"resume_only": True}, daemon=True).start()
        return "Продолжаю воспроизведение."

    # 4a. Переключение вывода на Яндекс Станцию («переключи на колонку», «музыку на колонку», «играй на колонке», «на станцию»)
    if any(
        p in t_low
        for p in (
            "на колонку",
            "на колонке",
            "на станцию",
            "на станции",
            "музыку на колонку",
            "музыку на станцию",
            "звук на колонку",
            "звук на станцию",
            "играй на колонке",
            "играй на станции",
            "включи на колонке",
            "включи на станции",
            "переключи на колонку",
            "переключи на станцию",
            "переведи на колонку",
            "переведи на станцию",
            "продолжи на колонке",
            "продолжи на станции",
        )
    ) and not any(w in t_low for w in ("громк", "тиш", "звук на 50", "процент", "секунд", "минут")):
        log.info("Команда переключения музыки на колонку: '%s'", t)
        threading.Thread(target=play_apple_music, kwargs={"target_device": "station", "resume_only": True}, daemon=True).start()
        return "Переключила музыку на колонку."

    # 4b. Переключение вывода на наушники («переключи в наушники», «музыку в наушники», «играй в наушниках», «в уши»)
    if any(
        p in t_low
        for p in (
            "в наушники",
            "в наушниках",
            "в уши",
            "в ушах",
            "музыку в наушники",
            "музыку в уши",
            "звук в наушники",
            "звук в уши",
            "играй в наушниках",
            "включи в наушниках",
            "переключи на наушники",
            "переключи в наушники",
            "переведи на наушники",
            "переведи в наушники",
            "продолжи в наушниках",
        )
    ):
        log.info("Команда переключения музыки в наушники: '%s'", t)
        threading.Thread(target=play_apple_music, kwargs={"target_device": "headphones", "resume_only": True}, daemon=True).start()
        return "Переключила музыку в наушники."

    # 5. Вопрос «что сейчас играет?»
    if _MUSIC_WHAT_RE.search(t_low):
        log.info("Команда что играет: '%s'", t)
        res = current_track_apple_music()
        track = res.get("track")
        artist = res.get("artist")
        if track and artist:
            return f"Сейчас играет {track} — {artist}."
        if track:
            return f"Сейчас играет {track}."
        return "Сейчас в Apple Music ничего не воспроизводится."

    # 6. Следующий трек
    if _MUSIC_NEXT_RE.search(t_low):
        log.info("Команда следующий трек: '%s'", t)
        res = next_apple_music()
        track = res.get("track")
        artist = res.get("artist")
        if track and artist:
            return f"Включаю следующий трек: {track} — {artist}."
        return "Включаю следующий трек."

    # 7. Перемотка назад / в начало / на N секунд
    if _MUSIC_REWIND_RE.search(t_low) or (t_low == "назад"):
        log.info("Команда перемотки назад: '%s'", t)
        if any(w in t_low for w in ("с начала", "в начало", "заново", "сначала")):
            res = restart_track_apple_music()
            track = res.get("track", "")
            return f"Включаю {track} с самого начала." if track else "Перемотала в начало трека."

        sec_m = re.search(r"\b(\d+)\s*(?:сек|секунд)", t_low)
        secs = int(sec_m.group(1)) if sec_m else 20
        res = rewind_apple_music(seconds=secs)
        if not res.get("ok"):
            return "Не удалось перемотать, сейчас ничего не играет."
        track = res.get("track", "")
        return f"Перемотала назад на {secs} секунд."

    # 8. Перемотка вперёд
    if _MUSIC_FORWARD_RE.search(t_low):
        log.info("Команда перемотки вперёд: '%s'", t)
        sec_m = re.search(r"\b(\d+)\s*(?:сек|секунд)", t_low)
        secs = int(sec_m.group(1)) if sec_m else 20
        res = forward_apple_music(seconds=secs)
        if not res.get("ok"):
            return "Не удалось перемотать, сейчас ничего не играет."
        return f"Перемотала вперёд на {secs} секунд."

    # 9. Предыдущий трек (с учётом особенностей радиопотока)
    if _MUSIC_PREV_RE.search(t_low):
        log.info("Команда предыдущий трек: '%s'", t)
        res = prev_apple_music()
        if not res.get("ok"):
            return "Сейчас в Apple Music ничего не играет."
        track = res.get("track")
        artist = res.get("artist")
        if res.get("action") == "radio_restart" or res.get("is_radio"):
            if track:
                return f"На радиостанции нельзя вернуться к прошлым песням. Перемотала {track} в начало."
            return "На радиостанции нельзя вернуться назад. Перемотала песню в начало."
        if track and artist:
            return f"Включаю: {track} — {artist}."
        return "Включаю предыдущий трек."

    # 10. Запуск музыки / моя волна / плейлист / жанры (рок, лоуфай, джаз)
    is_short_genre = len(t_low.split()) <= 4 and bool(
        re.search(
            r"\b(?:рок|rock|лоуфай|лоу-фай|lofi|lo-fi|джаз|jazz|киберпанк|cyberpunk|хотлайн|попсу|попса)\b",
            t_low,
        )
    )
    if (
        _MUSIC_PLAY_RE.search(t_low)
        or is_short_genre
        or t_low in ("музыка", "играй", "моя волна", "продолжи музыку", "продолжи", "сними с паузы")
        or ("apple music" in t_low or "эпл мюзик" in t_low or "эпл мьюзик" in t_low or "эплмьюзик" in t_low)
    ):
        target, label = _extract_target_music(t_low)
        want_station = any(w in t_low for w in ("на колонке", "на колонку", "на станции", "на станцию"))
        want_headphones = any(w in t_low for w in ("в наушниках", "в наушники", "в ушах", "в уши"))
        target_device = "station" if want_station else ("headphones" if want_headphones else "")

        log.info("Команда запуска Apple Music (target='%s', label='%s', device='%s'): '%s'", target, label, target_device, t)
        threading.Thread(target=play_apple_music, args=(target, False), kwargs={"target_device": target_device}, daemon=True).start()
        dest_str = " на колонке" if want_station else (" в наушниках" if want_headphones else "")
        if label:
            return f"Включаю {label}{dest_str} в Apple Music."
        return f"Включаю вашу станцию{dest_str} в Apple Music."

    return None


# --- Автоматический handoff наушники -> Яндекс Станция -----------------------

_HANDOFF_RUNNING = False


def _headphone_handoff_loop() -> None:
    """
    Фоновый наблюдатель: если в Bluetooth-наушниках играла музыка, и пользователь снял
    их (наушники отключились или воспроизведение встало на паузу при снятии с ушей),
    автоматически переключает вывод звука на Яндекс Станцию Мини 2 и продолжает трек.
    """
    log.info("Запущен фоновый мониторинг наушников для автопереключения на Яндекс Станцию")
    was_playing_on_headphones = False

    while True:
        try:
            time.sleep(2.0)
            res = _run_jxa("""
            (function() {
                var music = Application("Music");
                if (!music.running()) return JSON.stringify({running: false});
                var st = music.playerState();
                var devs = music.airplayDevices();
                var btSelected = false;
                var btCount = 0;
                for (var i = 0; i < devs.length; i++) {
                    if (devs[i].kind() === "Bluetooth device") {
                        btCount++;
                        if (devs[i].selected()) btSelected = true;
                    }
                }
                return JSON.stringify({
                    running: true,
                    state: st,
                    btSelected: btSelected,
                    btCount: btCount
                });
            })();
            """, timeout=4.0)

            if not res or not res.get("running"):
                was_playing_on_headphones = False
                continue

            state = res.get("state")
            bt_selected = res.get("btSelected", False)
            bt_count = res.get("btCount", 0)

            # Если музыка играет в Bluetooth-наушниках
            if state == "playing" and (bt_selected or bt_count > 0):
                was_playing_on_headphones = True
                continue

            # Если музыка только что играла в наушниках, но наушники сняли/отключились:
            if was_playing_on_headphones and (state in ("paused", "stopped") or bt_count == 0):
                log.info("Наушники сняты или отключены -> автопереключение музыки на Яндекс Станцию")
                was_playing_on_headphones = False
                # Переключаем вывод на Яндекс Станцию и продолжаем трек
                play_apple_music(resume_only=True, target_device="station")
                time.sleep(1.0)
                continue

            if state != "playing":
                was_playing_on_headphones = False

        except Exception as exc:
            log.debug("Ошибка в headphone_handoff_loop: %s", exc)
            time.sleep(3.0)


def start_headphone_handoff_monitor() -> None:
    """Запускает фоновый поток авто-передачи звука на колонку при снятии наушников."""
    global _HANDOFF_RUNNING
    if _HANDOFF_RUNNING:
        return
    _HANDOFF_RUNNING = True
    t = threading.Thread(target=_headphone_handoff_loop, daemon=True, name="headphone-handoff")
    t.start()
