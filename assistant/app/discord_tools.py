"""Голосовое управление звонком в Discord: микрофон, звук, вебка (камера) и демка (демонстрация экрана)."""

from __future__ import annotations

import ctypes
import json
import logging
import re
import subprocess
import time

from .mac_tools import capture_screen_base64

log = logging.getLogger(__name__)

# Загружаем CoreGraphics для аппаратных кликов мышью и нажатий клавиш
try:
    _cg = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )

    class _CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    _cg.CGMainDisplayID.restype = ctypes.c_uint32
    _cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    _cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    _cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    _cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]

    _cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        _CGPoint,
        ctypes.c_uint32,
    ]
    _cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    _cg.CGEventSetFlags.restype = None
    _cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    _cg.CGEventPost.restype = None
    _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _cg.CFRelease.restype = None
    _cg.CFRelease.argtypes = [ctypes.c_void_p]
    _HAS_CG = True
except Exception as exc:
    log.warning("CoreGraphics недоступен: %s", exc)
    _HAS_CG = False


def get_screen_size() -> tuple[float, float]:
    if _HAS_CG:
        did = _cg.CGMainDisplayID()
        w = float(_cg.CGDisplayPixelsWide(did))
        h = float(_cg.CGDisplayPixelsHigh(did))
        if w > 100 and h > 100:
            return w, h
    return 1710.0, 1107.0


def click_screen_point(x: float, y: float) -> None:
    """Выполняет настоящий аппаратный левый клик мышью в точке (x, y) через CoreGraphics."""
    if not _HAS_CG:
        return
    pt = _CGPoint(float(x), float(y))
    ev_move = _cg.CGEventCreateMouseEvent(None, 5, pt, 0)
    ev_down = _cg.CGEventCreateMouseEvent(None, 1, pt, 0)
    ev_up = _cg.CGEventCreateMouseEvent(None, 2, pt, 0)
    if ev_move:
        _cg.CGEventPost(0, ev_move)
        _cg.CFRelease(ev_move)
    time.sleep(0.04)
    if ev_down:
        _cg.CGEventPost(0, ev_down)
        _cg.CFRelease(ev_down)
    time.sleep(0.05)
    if ev_up:
        _cg.CGEventPost(0, ev_up)
        _cg.CFRelease(ev_up)


def press_key_cg(keycode: int, cmd: bool = False, shift: bool = False) -> None:
    """Отправляет одиночное нажатие клавиши (CoreGraphics, либо фоллбэк через System Events)."""
    if _HAS_CG:
        flags = 0
        if cmd:
            flags |= 1 << 20  # kCGEventFlagMaskCommand
        if shift:
            flags |= 1 << 17  # kCGEventFlagMaskShift
        ev_down = _cg.CGEventCreateKeyboardEvent(None, keycode, True)
        ev_up = _cg.CGEventCreateKeyboardEvent(None, keycode, False)
        if ev_down and ev_up:
            if flags:
                _cg.CGEventSetFlags(ev_down, flags)
                _cg.CGEventSetFlags(ev_up, flags)
            _cg.CGEventPost(0, ev_down)
            _cg.CFRelease(ev_down)
            time.sleep(0.04)
            _cg.CGEventPost(0, ev_up)
            _cg.CFRelease(ev_up)
            return

    mods = []
    if cmd:
        mods.append("command down")
    if shift:
        mods.append("shift down")
    using_clause = f" using {{{', '.join(mods)}}}" if mods else ""
    subprocess.run(
        ["osascript", "-e", f'tell application "System Events" to key code {keycode}{using_clause}'],
        capture_output=True,
        text=True,
        timeout=4,
    )


# --- Нативный Accessibility API (AXUIElement) через ctypes ------------------
# Работает прямо из процесса ассистента (у него есть право «Универсальный доступ»),
# в отличие от osascript/JXA, которому macOS этот доступ не выдаёт.

try:
    _cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    _ax = _cg  # ApplicationServices включает HIServices (AXUIElement*)

    class _CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    _UTF8 = 0x08000100
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFGetTypeID.restype = ctypes.c_ulong
    _cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    _cf.CFStringGetTypeID.restype = ctypes.c_ulong
    _cf.CFStringGetTypeID.argtypes = []
    _cf.CFArrayGetTypeID.restype = ctypes.c_ulong
    _cf.CFArrayGetTypeID.argtypes = []
    _cf.CFStringGetLength.restype = ctypes.c_long
    _cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFArrayGetCount.restype = ctypes.c_long
    _cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    _cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    _cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    _cf.CFRelease.restype = None
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _ax.AXIsProcessTrusted.restype = ctypes.c_bool
    _ax.AXIsProcessTrusted.argtypes = []
    _ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
    _ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
    _ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    _ax.AXUIElementCopyAttributeValue.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
    _ax.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _ax.AXUIElementPerformAction.restype = ctypes.c_int32
    _ax.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
    _ax.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
    _ax.AXValueGetValue.restype = ctypes.c_bool
    _ax.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    _kCFBooleanTrue = ctypes.c_void_p.in_dll(_cf, "kCFBooleanTrue")
    _cf.CFNumberGetTypeID.restype = ctypes.c_ulong
    _cf.CFNumberGetTypeID.argtypes = []
    _cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    _cf.CFBooleanGetTypeID.argtypes = []
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _cf.CFBooleanGetValue.restype = ctypes.c_bool
    _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    _CF_NUMBER_TYPE = _cf.CFNumberGetTypeID()
    _CF_BOOL_TYPE = _cf.CFBooleanGetTypeID()
    _CF_STRING_TYPE = _cf.CFStringGetTypeID()
    _CF_ARRAY_TYPE = _cf.CFArrayGetTypeID()
    _HAS_AX = _HAS_CG
except Exception as exc:  # pragma: no cover - только macOS
    log.warning("Accessibility API недоступен: %s", exc)
    _HAS_AX = False

_CFSTR_CACHE: dict[str, int] = {}

# Роли интерактивных элементов. Текст сообщений чата (AXStaticText / AXTextArea)
# сюда намеренно не входит — раньше поиск «камера» срабатывал на переписку.
_BUTTON_ROLES = {
    "AXButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXTab",
    "AXMenuItem",
    "AXPopUpButton",
    "AXMenuButton",
    "AXToggle",
    "AXSwitch",
    "AXLink",
}
_TILE_ROLES = _BUTTON_ROLES | {"AXGroup", "AXCell", "AXImage", "AXListItem"}


def _cfstr(text: str) -> int:
    ref = _CFSTR_CACHE.get(text)
    if ref is None:
        ref = _cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
        _CFSTR_CACHE[text] = ref
    return ref


def _cf_to_str(ref) -> str:
    if not ref or _cf.CFGetTypeID(ref) != _CF_STRING_TYPE:
        return ""
    length = _cf.CFStringGetLength(ref)
    size = length * 4 + 1
    buf = ctypes.create_string_buffer(size)
    if _cf.CFStringGetCString(ref, buf, size, _UTF8):
        return buf.value.decode("utf-8", errors="ignore")
    return ""


def _ax_copy(el, attr: str):
    val = ctypes.c_void_p()
    err = _ax.AXUIElementCopyAttributeValue(el, _cfstr(attr), ctypes.byref(val))
    if err != 0 or not val.value:
        return None
    return val.value


def _ax_str(el, attr: str) -> str:
    v = _ax_copy(el, attr)
    if not v:
        return ""
    try:
        return _cf_to_str(v)
    finally:
        _cf.CFRelease(v)


def _ax_checked(el) -> bool | None:
    """Состояние чекбокса/тоггла (AXValue): True/False, либо None если не число."""
    v = _ax_copy(el, "AXValue")
    if not v:
        return None
    try:
        tid = _cf.CFGetTypeID(v)
        if tid == _CF_BOOL_TYPE:
            return bool(_cf.CFBooleanGetValue(v))
        if tid == _CF_NUMBER_TYPE:
            out = ctypes.c_int32()
            if _cf.CFNumberGetValue(v, 3, ctypes.byref(out)):  # kCFNumberSInt32Type
                return out.value != 0
        return None
    finally:
        _cf.CFRelease(v)


def _ax_frame(el) -> tuple[float, float, float, float] | None:
    pos_ref = _ax_copy(el, "AXPosition")
    size_ref = _ax_copy(el, "AXSize")
    try:
        if not pos_ref or not size_ref:
            return None
        pt = _CGPoint()
        sz = _CGSize()
        if not _ax.AXValueGetValue(pos_ref, 1, ctypes.byref(pt)):  # kAXValueCGPointType
            return None
        if not _ax.AXValueGetValue(size_ref, 2, ctypes.byref(sz)):  # kAXValueCGSizeType
            return None
        return pt.x, pt.y, sz.width, sz.height
    finally:
        if pos_ref:
            _cf.CFRelease(pos_ref)
        if size_ref:
            _cf.CFRelease(size_ref)


def _discord_pid() -> int | None:
    try:
        res = subprocess.run(["pgrep", "-x", "Discord"], capture_output=True, text=True, timeout=3)
        pids = [int(p) for p in res.stdout.split() if p.strip().isdigit()]
        if pids:
            return min(pids)
    except Exception:
        pass
    return None


def ax_available() -> bool:
    return bool(_HAS_AX and _ax.AXIsProcessTrusted())


def _ax_find(
    keywords: list[str],
    roles: set[str] = _BUTTON_ROLES,
    press: bool = False,
    exclude: tuple[str, ...] = (),
    max_w: float = 700.0,
    max_h: float = 500.0,
) -> dict | None:
    """
    Обходит AX-дерево окна Discord и ищет интерактивный элемент, чья подпись
    (AXTitle / AXDescription / AXHelp) содержит одно из ключевых слов.
    Приоритет — порядок ключевых слов в списке. При press=True нажимает элемент
    (AXPress, а если не поддерживается — аппаратный клик по центру).
    """
    if not _HAS_AX:
        return None
    pid = _discord_pid()
    if not pid:
        return None

    kws = [k.lower() for k in keywords]
    excl = [e.lower() for e in exclude]
    app = _ax.AXUIElementCreateApplication(pid)
    if not app:
        return None
    _ax.AXUIElementSetMessagingTimeout(app, 1.5)
    # Electron строит дерево доступности только после этого флага
    _ax.AXUIElementSetAttributeValue(app, _cfstr("AXManualAccessibility"), _kCFBooleanTrue)

    held: list[int] = []  # CFArray, которые держат детей живыми до конца обхода
    best: dict | None = None
    best_rank = len(kws) + 1
    best_el = None
    started = time.perf_counter()
    visited = 0

    try:
        wins = _ax_copy(app, "AXWindows")
        if not wins or _cf.CFGetTypeID(wins) != _CF_ARRAY_TYPE:
            if wins:
                _cf.CFRelease(wins)
            return None
        held.append(wins)
        stack = [(_cf.CFArrayGetValueAtIndex(wins, i), 0) for i in range(_cf.CFArrayGetCount(wins))]

        while stack:
            if visited > 9000 or time.perf_counter() - started > 6.0:
                log.debug("AX-обход Discord прерван: узлов=%s", visited)
                break
            el, depth = stack.pop()
            visited += 1

            role = _ax_str(el, "AXRole")
            if role in roles:
                label = " ".join(
                    s for s in (_ax_str(el, "AXTitle"), _ax_str(el, "AXDescription"), _ax_str(el, "AXHelp")) if s
                ).strip()
                low = label.lower()
                if low and not any(e in low for e in excl):
                    for rank, k in enumerate(kws):
                        if rank >= best_rank:
                            break
                        if k in low:
                            frame = _ax_frame(el)
                            if frame and frame[2] > 4 and frame[3] > 4 and frame[2] <= max_w and frame[3] <= max_h:
                                x, y, w, h = frame
                                best = {
                                    "found": True,
                                    "label": label,
                                    "role": role,
                                    "checked": _ax_checked(el) if role in ("AXCheckBox", "AXToggle", "AXSwitch") else None,
                                    "x": x + w / 2.0,
                                    "y": y + h / 2.0,
                                    "left": x,
                                    "top": y,
                                    "w": w,
                                    "h": h,
                                }
                                best_rank = rank
                                best_el = el
                            break
                if best_rank == 0:
                    break

            if depth < 45:
                kids = _ax_copy(el, "AXChildren")
                if kids:
                    if _cf.CFGetTypeID(kids) == _CF_ARRAY_TYPE:
                        held.append(kids)
                        n = _cf.CFArrayGetCount(kids)
                        # в обратном порядке, чтобы обход шёл сверху вниз
                        for i in range(n - 1, -1, -1):
                            stack.append((_cf.CFArrayGetValueAtIndex(kids, i), depth + 1))
                    else:
                        _cf.CFRelease(kids)

        if best and press and best_el:
            err = _ax.AXUIElementPerformAction(best_el, _cfstr("AXPress"))
            if err != 0:
                click_screen_point(best["x"], best["y"])
            best["pressed"] = True
        if best:
            log.info(
                "AX Discord: '%s' (%s) %s, узлов=%s, %.2f с",
                best["label"],
                best["role"],
                "нажато" if press else "найдено",
                visited,
                time.perf_counter() - started,
            )
        return best
    finally:
        for ref in held:
            _cf.CFRelease(ref)
        _cf.CFRelease(app)


def _ax_wait(keywords: list[str], timeout: float = 4.0, **kwargs) -> dict | None:
    deadline = time.perf_counter() + timeout
    while True:
        hit = _ax_find(keywords, **kwargs)
        if hit or time.perf_counter() >= deadline:
            return hit
        time.sleep(0.35)


def _activate_discord() -> None:
    subprocess.run(["open", "-a", "Discord"], check=False, timeout=5)
    time.sleep(0.35)


# --- Запасной Vision-поиск (только если Accessibility недоступен) ------------

VISION_GROUNDING_PROMPT = (
    "Ты система компьютерного зрения для управления интерфейсом Discord на macOS. "
    "Найди на скриншоте запрошенную кнопку или элемент интерфейса и верни его координаты "
    "в нормализованной шкале от 0 до 1000 (где x=0..1000 слева направо, y=0..1000 сверху вниз). "
    "Если элемент найден, ответь СТРОГО одной строкой JSON без markdown: "
    '{"found": true, "x": 250, "y": 850} '
    'Если элемента нет на экране или ты не уверен, ответь: {"found": false}'
)


def _vision_click_discord_element(target_description: str, llm) -> dict | None:
    """Vision GUI grounding: скриншот → координаты кнопки → аппаратный клик."""
    if llm is None:
        return None
    img_b64, _ = capture_screen_base64()
    if not img_b64:
        return None
    try:
        raw = llm.chat_vision(
            system_prompt=VISION_GROUNDING_PROMPT,
            user_prompt=f"Найди на экране Discord элемент: {target_description}",
            image_b64=img_b64,
            num_predict=60,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        if not data.get("found"):
            return None
        sw, sh = get_screen_size()
        sx = (float(data["x"]) / 1000.0) * sw
        sy = (float(data["y"]) / 1000.0) * sh
        log.info("Vision grounding Discord click (%s): -> screen=(%.1f, %.1f)", target_description, sx, sy)
        click_screen_point(sx, sy)
        return {"found": True, "x": sx, "y": sy, "left": sx - 30, "top": sy - 15}
    except Exception as exc:
        log.warning("Ошибка Vision-поиска кнопки в Discord: %s", exc)
        return None


_NOT_IN_CALL = "Не нашла панель звонка в Дискорде. Сначала зайдите в голосовой канал или начните звонок."
_NO_AX = (
    "У ассистента нет доступа к интерфейсу Дискорда. Включите для Python право "
    "Универсальный доступ в настройках конфиденциальности мака."
)


# --- Микрофон и звук (чекбоксы «Заглушить» / «Откл. звук» в панели пользователя)

_MUTE_LABELS = [
    "заглушить",
    "разглушить",
    "включить микрофон",
    "выключить микрофон",
    "откл. микрофон",
    "отключить микрофон",
    "mute",
    "unmute",
]
_MUTE_EXCLUDE = (
    "больше настроек",
    "настроек",
    "параметры",
    "звук",
    "камер",
    "видео",
    "стрим",
)

_DEAFEN_LABELS = [
    "откл. звук",
    "отключить звук",
    "включить звук",
    "заглушить звук",
    "разглушить звук",
    "звук",
    "deafen",
    "undeafen",
]
_DEAFEN_EXCLUDE = (
    "больше настроек",
    "настроек",
    "параметры",
    "микрофон",
    "камер",
    "звуковая панель",
)


def _set_discord_checkbox(
    labels: list[str],
    want_on: bool | None,
    exclude: tuple[str, ...] = (),
    hotkey_code: int | None = None,
) -> bool | None:
    """
    Находит чекбокс Discord, читает его состояние и нажимает только при необходимости.
    Возвращает итоговое состояние (True = включён «Заглушить»/«Откл. звук»), либо None,
    если чекбокс не найден через Accessibility.
    """
    if not ax_available():
        return None

    # Закрываем случайные всплывающие меню (например, меню стрелочки настроек микрофона)
    press_key_cg(53)  # Escape
    time.sleep(0.05)

    # Приоритет 1: постоянный системный чекбокс в панели пользователя внизу слева (AXCheckBox)
    hit = _ax_find(labels, roles={"AXCheckBox"}, exclude=exclude)
    # Приоритет 2: кнопка в активном звонке
    if not hit:
        hit = _ax_find(labels, roles={"AXToggle", "AXSwitch", "AXButton"}, exclude=exclude)
    if not hit:
        return None

    state = hit.get("checked")
    if want_on is not None and state is not None and state == want_on:
        log.info("Discord чекбокс '%s' уже в состоянии %s, переключение не требуется", hit.get("label"), state)
        return state

    # Нажимаем целевой чекбокс через AXPress
    _ax_find(labels, roles={hit["role"]}, press=True, exclude=exclude)
    time.sleep(0.25)

    after = _ax_find(labels, roles={hit["role"]}, exclude=exclude)
    if after and after.get("checked") is not None:
        state_after = after["checked"]
        if want_on is None or state_after == want_on:
            return state_after

    # Если через AX состояние не поменялось, используем глобальный хоткей Discord
    if hotkey_code is not None:
        press_key_cg(hotkey_code, cmd=True, shift=True)
        time.sleep(0.25)
        after_hotkey = _ax_find(labels, roles={"AXCheckBox"}, exclude=exclude)
        if after_hotkey and after_hotkey.get("checked") is not None:
            return after_hotkey["checked"]

    return want_on if want_on is not None else (not state if state is not None else None)


def discord_toggle_mute(mute: bool | None = None) -> str:
    """Включает или выключает микрофон в Discord (по состоянию чекбокса; хоткей Cmd+Shift+M — запасной)."""
    _activate_discord()
    state = _set_discord_checkbox(_MUTE_LABELS, mute, exclude=_MUTE_EXCLUDE, hotkey_code=46)
    if state is None:
        press_key_cg(46, cmd=True, shift=True)
        state = mute
    if state is True:
        return "Замьютила микрофон в Дискорде."
    if state is False:
        return "Включила микрофон в Дискорде."
    return "Переключила микрофон в Дискорде."


def discord_toggle_deafen(deafen: bool | None = None) -> str:
    """Включает или выключает звук (Deafen) в Discord (по состоянию чекбокса; Cmd+Shift+D — запасной)."""
    _activate_discord()
    state = _set_discord_checkbox(_DEAFEN_LABELS, deafen, exclude=_DEAFEN_EXCLUDE, hotkey_code=2)
    if state is None:
        press_key_cg(2, cmd=True, shift=True)
        state = deafen
    if state is True:
        return "Отключила звук в Дискорде."
    if state is False:
        return "Включила звук в Дискорде."
    return "Переключила звук в Дискорде."


# --- Веб-камера ----------------------------------------------------------------

_CAM_ON_LABELS = ["выключить камеру", "отключить камеру", "turn off camera", "stop video", "остановить видео"]
_CAM_OFF_LABELS = ["включить камеру", "turn on camera", "start video", "включить видео"]


def discord_toggle_camera(enable: bool | None = None, llm=None) -> str:
    """Включает или выключает веб-камеру в звонке Discord, учитывая её текущее состояние."""
    _activate_discord()

    if not ax_available():
        hit = _vision_click_discord_element(
            "кнопка камеры (Включить камеру / Выключить камеру) в панели голосового звонка Discord", llm
        )
        return "Попробовала переключить вебку в Дискорде." if hit else _NO_AX

    hit = _ax_find(_CAM_ON_LABELS + _CAM_OFF_LABELS, exclude=("видеозвон", "video call"))
    if not hit:
        hit = _ax_find(["камера", "camera"], exclude=("видеозвон", "video call", "настройк", "settings"))
    if not hit:
        return _NOT_IN_CALL

    low = hit["label"].lower()
    is_on = any(k in low for k in _CAM_ON_LABELS)
    if enable is True and is_on:
        return "Вебка в Дискорде уже включена."
    if enable is False and not is_on and any(k in low for k in _CAM_OFF_LABELS):
        return "Вебка в Дискорде уже выключена."

    _ax_find([hit["label"]], press=True)
    time.sleep(1.0)

    after = _ax_find(_CAM_ON_LABELS + _CAM_OFF_LABELS, exclude=("видеозвон", "video call"))
    now_on = bool(after and any(k in after["label"].lower() for k in _CAM_ON_LABELS))
    if now_on == is_on:
        return "Нажала кнопку камеры, но состояние не изменилось. Проверьте Дискорд."
    return "Включила вебку в Дискорде." if now_on else "Выключила вебку в Дискорде."


# --- Демонстрация экрана («демка») --------------------------------------------

_SHARE_STOP_LABELS = [
    "прекратить трансляцию",
    "остановить трансляцию",
    "прекратить стрим",
    "остановить демонстрацию",
    "прекратить демонстрацию",
    "stop streaming",
    "stop sharing",
]
_SHARE_START_LABELS = [
    "продемонстрировать свой экран",
    "продемонстрировать экран",
    "демонстрация экрана",
    "поделиться экраном",
    "share your screen",
    "share screen",
]
_GO_LIVE_LABELS = ["прямой эфир", "go live", "начать трансляцию", "транслировать"]


def discord_start_screen_share(llm=None) -> str:
    """
    Включает демонстрацию всего экрана в Discord:
    1) кнопка «Демонстрация экрана» в панели звонка;
    2) вкладка «Экраны» и превью экрана в окне выбора;
    3) кнопка «Прямой эфир» (Go Live) и проверка, что трансляция началась.
    """
    _activate_discord()

    if not ax_available():
        hit = _vision_click_discord_element(
            "кнопка демонстрации экрана (иконка монитора со стрелкой) в панели звонка Discord", llm
        )
        return "Попробовала открыть демонстрацию экрана в Дискорде." if hit else _NO_AX

    if _ax_find(_SHARE_STOP_LABELS):
        return "Демонстрация экрана в Дискорде уже идёт."

    if not _ax_find(_SHARE_START_LABELS, press=True):
        return _NOT_IN_CALL

    go_live = _ax_wait(_GO_LIVE_LABELS, timeout=4.0)
    if not go_live:
        return (
            "Открыла выбор демонстрации экрана в Дискорде. "
            "Окно выбора не распознано — выберите экран вручную."
        )

    # Вкладка «Экраны» (если Discord показывает вкладки Приложения / Экраны)
    if _ax_find(["экраны", "screens", "весь экран", "entire screen"], roles=_BUTTON_ROLES, press=True):
        time.sleep(0.5)

    # Превью экрана
    _ax_find(
        ["весь экран", "entire screen", "экран 1", "screen 1", "display 1", "встроенный", "built-in", "color lcd", "экран"],
        roles=_TILE_ROLES,
        press=True,
        exclude=("экраны", "screens", "демонстрац", "прямой эфир", "go live"),
    )
    time.sleep(0.4)

    if not _ax_find(_GO_LIVE_LABELS, press=True):
        press_key_cg(36)  # Enter как запасной вариант

    if _ax_wait(_SHARE_STOP_LABELS, timeout=4.0):
        return "Включила демонстрацию всего экрана в Дискорде."
    return "Нажала Прямой эфир, но не вижу, что трансляция началась. Проверьте окно Дискорда."


def discord_stop_screen_share(llm=None) -> str:
    """Останавливает демонстрацию экрана («демку») в Discord."""
    _activate_discord()
    if not ax_available():
        hit = _vision_click_discord_element(
            "кнопка «Прекратить стрим» (Stop Streaming) в панели звонка Discord", llm
        )
        return "Попробовала выключить демонстрацию экрана." if hit else _NO_AX
    if _ax_find(_SHARE_STOP_LABELS, press=True):
        return "Выключила демонстрацию экрана в Дискорде."
    return "Не нашла активную демонстрацию экрана в Дискорде."


def discord_disconnect_call(llm=None) -> str:
    """Отключается от голосового канала / звонка в Discord."""
    _activate_discord()
    if not ax_available():
        hit = _vision_click_discord_element("красная кнопка «Отключиться» (Disconnect) в панели звонка Discord", llm)
        return "Попробовала отключиться от звонка." if hit else _NO_AX
    if _ax_find(["отключиться", "disconnect", "покинуть голосовой канал", "завершить звонок", "leave call"], press=True):
        return "Отключилась от звонка в Дискорде."
    return "Не нашла кнопку отключения от звонка в Дискорде."


def try_handle_discord_call_command(t: str, t_lower: str, llm=None) -> str | None:
    """
    Распознаёт голосовые команды управления звонком в Discord:
    - включи/выключи демку (демонстрацию экрана);
    - включи/выключи микрофон (замьють/размьють/замуть/размуть);
    - включи/выключи звук в звонке (заглуши/разглуши/отключи звук);
    - включи/выключи вебку (камеру);
    - выйди из звонка / отключись от звонка.
    """
    # 1. Демка / демонстрация экрана / стрим
    if any(w in t_lower for w in ("демк", "демонстрац", "стрим", "шаринг", "показ экрана")):
        if any(w in t_lower for w in ("выключ", "останов", "прекрат", "закрой", "выруби")):
            log.info("Команда Discord stop_screen_share: '%s'", t)
            return discord_stop_screen_share(llm=llm)
        if any(w in t_lower for w in ("включ", "запуст", "начн", "открой", "покаж", "вруби")):
            log.info("Команда Discord start_screen_share: '%s'", t)
            return discord_start_screen_share(llm=llm)

    # 2. Полное отключение звука в Discord (Deafen — «разглуши», «заглуши», «отключи звук в звонке»)
    if any(w in t_lower for w in ("разглуши", "разглушить")):
        log.info("Команда Discord undeafen: '%s'", t)
        return discord_toggle_deafen(deafen=False)

    if any(w in t_lower for w in ("заглуши", "заглушить")) and "микрофон" not in t_lower:
        log.info("Команда Discord deafen: '%s'", t)
        return discord_toggle_deafen(deafen=True)

    if "звук" in t_lower and any(w in t_lower for w in ("звонк", "дс", "дискорд", "discord", "разговор")):
        if any(w in t_lower for w in ("выключ", "выруби", "отключ", "замуть", "замьют")):
            log.info("Команда Discord deafen: '%s'", t)
            return discord_toggle_deafen(deafen=True)
        if any(w in t_lower for w in ("включ", "вруби", "размуть", "размьют", "верни")):
            log.info("Команда Discord undeafen: '%s'", t)
            return discord_toggle_deafen(deafen=False)

    # 3. Микрофон (замьють/размьють, замуть/размуть, выключи/включи микрофон)
    if any(w in t_lower for w in ("микрофон", "микро", "замуть", "размуть", "замьють", "размьють", "мут", "мьют")):
        if any(w in t_lower for w in ("размуть", "размьють", "включ", "вруби", "разглуши")) and not any(
            v in t_lower for v in ("выключ", "отключ", "выруби", "замуть", "замьють")
        ):
            log.info("Команда Discord unmute: '%s'", t)
            return discord_toggle_mute(mute=False)
        if any(w in t_lower for w in ("замуть", "замьють", "выключ", "выруби", "отключ", "мут", "мьют")):
            log.info("Команда Discord mute: '%s'", t)
            return discord_toggle_mute(mute=True)

    # 4. Вебка / камера в Discord
    if any(w in t_lower for w in ("вебк", "веб-камер", "вебкамер", "камер")):
        if any(w in t_lower for w in ("выключ", "выруби", "отключ", "закрой")):
            log.info("Команда Discord camera OFF: '%s'", t)
            return discord_toggle_camera(enable=False, llm=llm)
        if any(w in t_lower for w in ("включ", "вруби", "запуст", "открой")):
            log.info("Команда Discord camera ON: '%s'", t)
            return discord_toggle_camera(enable=True, llm=llm)

    # 5. Выход из звонка / голосового канала
    if any(
        p in t_lower
        for p in (
            "выйди из звонка",
            "отключись от звонка",
            "выйди из голосового",
            "покинь звонок",
            "заверши звонок",
            "отключись от дс",
            "выйди из дс",
            "отключись от дискорда",
            "выйди из дискорда",
            "сбрось звонок в дискорде",
        )
    ):
        return discord_disconnect_call(llm=llm)

    return None
