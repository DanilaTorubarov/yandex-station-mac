"""Голосовые действия для разработчика, терминала Warp, Docker и Git."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HOME = Path.home()
CODES_DIR = HOME / "Codes"
MINECRAFT_DIR = CODES_DIR / "minecraft"
DEFAULT_REPO_DIR = CODES_DIR / "yandex-station-mini2-toolkit"

# Полный PATH для launchd окружения macOS
EXPANDED_PATH = (
    f"{HOME}/.docker/bin:"
    f"{HOME}/.cargo/bin:"
    "/opt/homebrew/bin:"
    "/usr/local/bin:"
    + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
)


def _get_exec_env() -> dict[str, str]:
    """Возвращает переменные окружения с полным PATH для dev-инструментов."""
    env = os.environ.copy()
    env["PATH"] = EXPANDED_PATH
    return env


# ==============================================================================
# 1. WARP TERMINAL: SSH К СЕРВЕРУ МИШИ / МАЙНКРАФТ
# ==============================================================================

def _connect_warp_ssh_worker() -> None:
    """
    Открывает Warp, создаёт новую вкладку (Cmd+T) и запускает SSH к серверу Миши.
    Пароль ключа id_ed25519 (ilovesasha) сохранён в macOS Keychain (UseKeychain yes),
    поэтому SSH подключается мгновенно без зависания.
    """
    try:
        # Проверяем, запущен ли Warp
        res = subprocess.run(["pgrep", "-ix", "Warp"], capture_output=True, text=True)
        is_running = res.returncode == 0

        if not is_running:
            log.info("Запуск Warp...")
            subprocess.Popen(["open", "-a", "Warp"])
            time.sleep(1.8)
        else:
            subprocess.run(["open", "-a", "Warp"])
            time.sleep(0.4)

        # AppleScript: активировать Warp, открыть новую вкладку (Cmd+T), ввести команду SSH
        script = """
        tell application "Warp" to activate
        delay 0.4
        tell application "System Events"
            tell process "Warp"
                keystroke "t" using {command down}
                delay 0.6
                keystroke "ssh cyclicoff@46.138.249.160"
                key code 36
            end tell
        end tell
        """
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10.0)
        log.info("Warp SSH команда cyclicoff@46.138.249.160 введена")
    except Exception as exc:
        log.warning("Ошибка при подключении к SSH в Warp: %s", exc)


def connect_misha_server_warp() -> str:
    """Запускает подключение к серверу Миши в терминале Warp в фоне."""
    threading.Thread(target=_connect_warp_ssh_worker, daemon=True).start()
    return "Открыла Warp и подключилась к серверу Миши."


# ==============================================================================
# 2. DOCKER & ЛОКАЛЬНЫЙ СЕРВЕР MINECRAFT
# ==============================================================================

def is_docker_running() -> bool:
    """Проверяет доступность Docker демона."""
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=3.0, env=_get_exec_env())
        return res.returncode == 0
    except Exception:
        return False


def ensure_docker_running(timeout_sec: int = 30) -> bool:
    """Проверяет и при необходимости запускает Docker Desktop."""
    if is_docker_running():
        return True

    log.info("Docker Desktop не запущен. Запускаем приложение...")
    try:
        subprocess.Popen(["open", "-a", "Docker"])
    except Exception as exc:
        log.warning("Не удалось открыть Docker: %s", exc)
        return False

    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        time.sleep(2.0)
        if is_docker_running():
            log.info("Docker Desktop успешно запустился и готов к работе")
            return True

    log.warning("Таймаут ожидания старта Docker Desktop (%d с)", timeout_sec)
    return is_docker_running()


def _start_minecraft_docker_worker() -> None:
    """Фоновый запуск контейнеров Minecraft."""
    cmd = ["docker", "compose", "up", "-d"]
    try:
        res = subprocess.run(
            cmd,
            cwd=str(MINECRAFT_DIR),
            capture_output=True,
            text=True,
            timeout=60.0,
            env=_get_exec_env(),
        )
        if res.returncode == 0:
            log.info("Локальный сервер Minecraft успешно запущен: %s", res.stdout.strip())
        else:
            log.warning("Ошибка docker compose: %s", res.stderr.strip())
    except Exception as exc:
        log.error("Исключение при старте Minecraft Docker: %s", exc)


def start_local_minecraft_server() -> str:
    """Запускает Docker Desktop (если не запущен) и проект в ~/Codes/minecraft."""
    if not MINECRAFT_DIR.exists():
        return f"Папка проекта {MINECRAFT_DIR} не найдена."

    # Убеждаемся, что есть .env
    env_file = MINECRAFT_DIR / ".env"
    env_docker = MINECRAFT_DIR / ".env.docker"
    if not env_file.exists() and env_docker.exists():
        try:
            os.symlink(".env.docker", env_file)
        except Exception:
            pass

    started_docker = False
    if not is_docker_running():
        started_docker = True
        ok = ensure_docker_running(timeout_sec=25)
        if not ok:
            return "Не удалось запустить Docker Desktop. Проверьте приложение Docker."

    # Запускаем docker compose в фоне, чтобы ассистент мгновенно ответил голосом
    threading.Thread(target=_start_minecraft_docker_worker, daemon=True).start()

    if started_docker:
        return "Запустила Docker и поднимаю локальный сервер Майнкрафт."
    return "Поднимаю локальный сервер Майнкрафт."


# ==============================================================================
# 3. GIT И ТЕСТЫ В ПРОЕКТАХ
# ==============================================================================

def detect_target_project(text: str) -> Path:
    """Определяет целевую папку проекта по запросу пользователя или контексту."""
    t_low = text.lower()
    if any(w in t_low for w in ("майнкрафт", "майн", "minecraft")):
        if MINECRAFT_DIR.exists():
            return MINECRAFT_DIR
    if any(w in t_low for w in ("станци", "toolkit", "assistant", "колонк")):
        candidate = CODES_DIR / "yandex-station-mini2-toolkit"
        if candidate.exists():
            return candidate

    # Проверяем активное окно VS Code / Cursor / Warp
    try:
        script = """
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            set winTitle to ""
            try
                set winTitle to name of front window of frontApp
            end try
            return winTitle
        end tell
        """
        res = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=2.0)
        win_title = res.stdout.strip().lower()
        if "minecraft" in win_title and MINECRAFT_DIR.exists():
            return MINECRAFT_DIR
        for p in CODES_DIR.iterdir():
            if p.is_dir() and p.name.lower() in win_title:
                return p
    except Exception:
        pass

    return DEFAULT_REPO_DIR


def git_commit(message: str, project_dir: Path | None = None) -> str:
    """Добавляет все файлы и создаёт коммит с переданным сообщением."""
    target = project_dir or DEFAULT_REPO_DIR
    clean_msg = message.strip(" \"'")
    if not clean_msg:
        clean_msg = "update"

    env = _get_exec_env()
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(target), capture_output=True, timeout=10.0, env=env)
        res = subprocess.run(
            ["git", "commit", "-m", clean_msg],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=10.0,
            env=env,
        )
        if res.returncode == 0:
            log.info("Git commit создан в %s: '%s'", target.name, clean_msg)
            return f"Закоммитила изменения с сообщением: {clean_msg}."
        if "nothing to commit" in res.stdout.lower() or "nothing to commit" in res.stderr.lower():
            return "Нет изменений для коммита в репозитории."
        err = (res.stderr or res.stdout).strip()
        log.warning("Ошибка git commit: %s", err)
        return f"Не удалось сделать коммит: {err[:80]}."
    except Exception as exc:
        log.warning("Исключение при git commit: %s", exc)
        return f"Ошибка коммита: {exc}."


def git_push(project_dir: Path | None = None) -> str:
    """Выполняет git push в текущей ветке."""
    target = project_dir or DEFAULT_REPO_DIR
    env = _get_exec_env()
    try:
        log.info("Выполняется git push в %s...", target.name)
        res = subprocess.run(
            ["git", "push"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=30.0,
            env=env,
        )
        if res.returncode == 0:
            return "Изменения успешно отправлены на удалённый репозиторий."
        err = (res.stderr or res.stdout).strip()
        log.warning("Ошибка git push: %s", err)
        return f"Не удалось отправить изменения: {err[:80]}."
    except Exception as exc:
        log.warning("Исключение при git push: %s", exc)
        return f"Ошибка отправки изменений: {exc}."


def run_project_tests(project_dir: Path | None = None) -> str:
    """Определяет сборщик/тест-раннер и запускает тесты проекта."""
    target = project_dir or DEFAULT_REPO_DIR
    env = _get_exec_env()

    test_cmd = None
    if (target / "Cargo.toml").exists():
        test_cmd = ["cargo", "test"]
    elif (target / "package.json").exists():
        test_cmd = ["npm", "test"]
    elif (target / "pytest.ini").exists() or (target / "tests").exists() or (target / "scripts" / "tests").exists():
        test_cmd = ["python3", "-m", "pytest"]
    elif (target / "Makefile").exists():
        test_cmd = ["make", "test"]

    if not test_cmd:
        return f"В проекте {target.name} не найден стандартный запуск тестов."

    log.info("Запуск тестов (%s) в %s...", " ".join(test_cmd), target.name)
    try:
        res = subprocess.run(
            test_cmd,
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=45.0,
            env=env,
        )
        out = (res.stdout + "\n" + res.stderr).strip()
        if res.returncode == 0:
            m = re.search(r"(\d+)\s+passed", out, re.I)
            if m:
                return f"Все тесты успешно пройдены: {m.group(1)} тестов без ошибок."
            m_cargo = re.search(r"test result:\s*ok\.\s*(\d+)\s*passed", out, re.I)
            if m_cargo:
                return f"Тесты успешно завершены: {m_cargo.group(1)} тестов пройдено."
            return "Тесты проекта успешно выполнены без ошибок."

        m_failed = re.search(r"(\d+)\s+failed", out, re.I)
        if m_failed:
            return f"Тесты не прошли: упало {m_failed.group(1)} тестов."
        return "Тесты завершились с ошибкой."
    except subprocess.TimeoutExpired:
        return "Время выполнения тестов превысило лимит."
    except Exception as exc:
        return f"Не удалось запустить тесты: {exc}."


def git_status_summary(project_dir: Path | None = None, llm=None) -> str:
    """
    Формирует понятную человеческую сводку git status и git diff для голосового ответа.
    «В проекте изменено 3 файла, добавлен модуль торрентов и обновлены настройки».
    """
    target = project_dir or DEFAULT_REPO_DIR
    env = _get_exec_env()

    if not (target / ".git").exists():
        return f"Каталог {target.name} не является git-репозиторием."

    try:
        # 1. Получаем статус файлов
        res_status = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=8.0,
            env=env,
        )
        status_out = res_status.stdout.strip()

        # 2. Проверяем, чистый ли репозиторий
        if not status_out:
            res_branch = subprocess.run(
                ["git", "status", "-sb"],
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=5.0,
                env=env,
            )
            branch_out = res_branch.stdout.strip()
            m_ahead = re.search(r"ahead\s+(\d+)", branch_out)
            if m_ahead:
                cnt = m_ahead.group(1)
                return f"В проекте {target.name} все файлы закоммичены, но есть {cnt} незапушенных коммитов."
            return f"В проекте {target.name} всё чисто, незакоммиченных изменений нет."

        # 3. Собираем статистику изменений (diff stat)
        res_stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=8.0,
            env=env,
        )
        stat_out = res_stat.stdout.strip()

        # 4. Компактный diff для смыслового анализа
        res_diff = subprocess.run(
            ["git", "diff", "-U1"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=8.0,
            env=env,
        )
        diff_sample = res_diff.stdout[:2500].strip()

        # 5. Если доступна LLM — генерируем живой человеческий пересказ
        if llm:
            prompt = (
                f"Ты голосовой ассистент разработчика на Яндекс Станции.\n"
                f"Перед тобой текущий статус и диф репозитория «{target.name}»:\n\n"
                f"[git status]\n{status_out}\n\n"
                f"[git diff --stat]\n{stat_out}\n\n"
            )
            if diff_sample:
                prompt += f"[фрагмент diff]\n{diff_sample}\n\n"

            prompt += (
                "Твоя задача: Расскажи кратко и человеческим языком, что изменилось в коде.\n"
                "Правила:\n"
                "- Ровно 1-2 ёмких предложения на русском языке.\n"
                "- Ответ сразу пойдёт в синтез речи (TTS), поэтому говори живо, естественно и без канцеляризмов.\n"
                "- СТРОГО никаких звёздочек, markdown, списков, бэктиков и технического мусора.\n"
                "- Пример хорошего ответа: «В проекте изменено 3 файла, добавлен модуль торрентов и обновлены настройки»."
            )
            try:
                summary = llm.chat([{"role": "user", "content": prompt}])
                if summary and len(summary.strip()) > 10:
                    clean_sum = summary.strip(" *`_#\n\t")
                    log.info("LLM Git summary для %s: '%s'", target.name, clean_sum)
                    return clean_sum
            except Exception as exc:
                log.warning("Ошибка генерации LLM git summary: %s", exc)

        # 6. Детерминированный fallback при отсутствии ответа LLM
        lines = [line.strip() for line in status_out.splitlines() if line.strip()]
        mod_files = []
        new_files = []
        for l in lines:
            parts = l.split(maxsplit=1)
            if len(parts) == 2:
                code, fname = parts
                base = Path(fname).name
                if "??" in code:
                    new_files.append(base)
                else:
                    mod_files.append(base)

        total_cnt = len(lines)
        detail_parts = []
        if mod_files:
            detail_parts.append(f"изменено {len(mod_files)} файлов ({', '.join(mod_files[:3])})")
        if new_files:
            detail_parts.append(f"добавлено {len(new_files)} новых файлов ({', '.join(new_files[:3])})")

        return f"В проекте {target.name} {total_cnt} изменённых файлов: {', '.join(detail_parts)}."
    except Exception as exc:
        log.warning("Исключение при получении git summary: %s", exc)
        return f"Не удалось прочитать статус репозитория: {exc}."


# ==============================================================================
# 4. ДИСПЕТЧЕР ГОЛОСОВЫХ КОМАНД
# ==============================================================================

_RE_MISHA_SSH = re.compile(
    r"\b(подключись|зайди|открой|войди)\b.*?\b(?:к\s+)?(?:сервак\w*|сервер\w*)\b.*?\b(?:миш\w*|майнкрафт\w*|майн\w*)\b",
    re.IGNORECASE,
)

_RE_LOCAL_MINECRAFT = re.compile(
    r"\b(подними|запусти|стартуй|включи)\b.*?\b(?:локальн\w*\s+)?(?:сервак\w*|сервер\w*)\b.*?\b(?:майн\w*|майнкрафт\w*)\b|\b(?:подними|запусти)\s+майн\b",
    re.IGNORECASE,
)

_RE_GIT_STATUS = re.compile(
    r"\b("
    r"что\s+(?:изменилось|поменялось|нового)\s+(?:в\s+)?(?:коде|проекте|репозитори\w*|гит\w*|git|ветк\w*|файл\w*)|"
    r"что\s+(?:я\s+)?(?:изменил|поменял)(?:\s+в\s+(?:коде|проекте|репозитори\w*|гит\w*|git))?|"
    r"(?:какой\s+)?статус\s+(?:репозитори\w*|проекта|гита|git|кода)|"
    r"гит\s+статус|git\s+status|гит\s+дифф|git\s+diff|"
    r"что\s+в\s+(?:гит\w*|git|репозитори\w*)|"
    r"сводк\w*\s+(?:изменений|по\s+коду|репозитори\w*|гита)|"
    r"код\s+ревью|code\s+review"
    r")\b",
    re.IGNORECASE,
)

_RE_GIT_COMMIT = re.compile(
    r"\b(?:закоммить|сделай\s+коммит|коммит)\b",
    re.IGNORECASE,
)

_RE_GIT_PUSH = re.compile(
    r"\b(?:git\s+push|гит\s+пуш|запушь|сделай\s+пуш|отправь\s+коммит\w*|запушь\s+изменени\w*)\b",
    re.IGNORECASE,
)

_RE_RUN_TESTS = re.compile(
    r"\b(?:запусти\s+тест\w*|прогони\s+тест\w*|выполни\s+тест\w*|тесты\s+в\s+проект\w*)\b",
    re.IGNORECASE,
)


def extract_commit_message(text: str) -> str:
    """Извлекает текст сообщения для коммита."""
    t = text.strip()
    m = re.search(r"(?:с\s+сообщением|с\s+текстом|сообщением|сообщение|текстом|текст)\s*[:\-]?\s*(.+)", t, re.I)
    if m:
        msg = m.group(1).strip()
        msg = msg.strip("\"'")
        if msg:
            return msg
    t_clean = _RE_GIT_COMMIT.sub("", t).strip(" :,\"-'")
    return t_clean or "update"


def try_handle_dev_command(text: str, llm=None) -> str | None:
    """
    Основная точка входа для команд разработки, терминала Warp, Docker и Git.
    """
    t_low = (text or "").strip().lower()

    # 1. SSH к серверу Миши / Майнкрафт в терминале Warp
    if _RE_MISHA_SSH.search(t_low) or ("сервак" in t_low and "миш" in t_low) or ("сервер" in t_low and "миш" in t_low):
        return connect_misha_server_warp()

    # 2. Поднятие локального сервера Minecraft через Docker
    if _RE_LOCAL_MINECRAFT.search(t_low) or (("подними" in t_low or "запусти" in t_low) and "майн" in t_low and "сервер" in t_low):
        return start_local_minecraft_server()

    # 3. Git status / Code Review сводка изменений голосом
    if _RE_GIT_STATUS.search(t_low):
        target_dir = detect_target_project(text)
        return git_status_summary(target_dir, llm=llm)

    # 4. Git commit
    if _RE_GIT_COMMIT.search(t_low):
        target_dir = detect_target_project(text)
        commit_msg = extract_commit_message(text)
        return git_commit(commit_msg, target_dir)

    # 5. Git push
    if _RE_GIT_PUSH.search(t_low):
        target_dir = detect_target_project(text)
        return git_push(target_dir)

    # 6. Запуск тестов
    if _RE_RUN_TESTS.search(t_low):
        target_dir = detect_target_project(text)
        return run_project_tests(target_dir)

    return None
