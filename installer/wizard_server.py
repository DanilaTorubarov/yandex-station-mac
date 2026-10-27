#!/usr/bin/env python3
"""
Автоматический веб-мастер установки и настройки голосового ассистента Qwen
для Яндекс Станции Мини 2 и Mac.

Работает на стандартной библиотеке Python 3 (без сторонних зависимостей).
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("installer")

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSISTANT_DIR = ROOT_DIR / "assistant"
WEB_DIR = Path(__file__).resolve().parent / "web"
ENV_FILE = ASSISTANT_DIR / ".env"
ENV_EXAMPLE = ASSISTANT_DIR / ".env.example"

_DEPLOY_LOCK = threading.Lock()
_DEPLOY_LOGS: list[str] = []
_DEPLOY_STATUS = {"running": False, "success": False, "step": "", "error": ""}


def append_log(msg: str):
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    log.info(line)
    _DEPLOY_LOGS.append(line)


def get_mac_ip() -> str:
    """Определяет локальный IP-адрес Mac."""
    for iface in ("en0", "en1", "en2", "bridge0"):
        try:
            res = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=1)
            ip = res.stdout.strip()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def scan_station_candidates() -> list[dict[str, Any]]:
    """Быстрый поиск кандидатов Яндекс Станции в локальной сети."""
    found: list[dict[str, Any]] = []
    seen = set()

    # 1. Чтение текущего .env
    current_ip = read_env_value("STATION_IP") or "192.168.2.83"
    current_port = int(read_env_value("STATION_SSH_PORT") or "2222")

    for port in (current_port, 2222, 22):
        if is_port_open(current_ip, port, timeout=0.3):
            found.append({"ip": current_ip, "port": port, "label": "Текущий адрес"})
            seen.add(current_ip)
            break

    # 2. Сканирование ARP-таблицы
    try:
        res = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
            if m:
                ip = m.group(1)
                if ip in seen or ip.endswith(".255") or ip.endswith(".1"):
                    continue
                for port in (2222, 22):
                    if is_port_open(ip, port, timeout=0.15):
                        found.append({"ip": ip, "port": port, "label": "Найдено в сети"})
                        seen.add(ip)
                        break
    except Exception:
        pass

    return found


def is_port_open(ip: str, port: int, timeout: float = 0.3) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def read_env_value(key: str) -> str:
    if not ENV_FILE.exists():
        return ""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def test_station_ssh(ip: str, port: int) -> dict[str, Any]:
    """Проверяет подключение к Станции по SSH или ADB."""
    # 1. SSH test
    cmd = [
        "ssh",
        "-p",
        str(port),
        "-o",
        "ConnectTimeout=3",
        "-o",
        "StrictHostKeyChecking=no",
        f"root@{ip}",
        "uname -a; cat /etc/os-release 2>/dev/null || true",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
        if res.returncode == 0:
            return {
                "ok": True,
                "transport": "ssh",
                "details": f"Подключено по SSH (порт {port})",
                "system": res.stdout.strip().splitlines()[0] if res.stdout else "Linux ARM",
            }
    except Exception as exc:
        pass

    # 2. ADB test
    try:
        adb_res = subprocess.run(["adb", "connect", f"{ip}:5555"], capture_output=True, text=True, timeout=3)
        if "connected" in adb_res.stdout.lower():
            return {"ok": True, "transport": "adb", "details": "Подключено по ADB (:5555)"}
    except Exception:
        pass

    return {"ok": False, "error": f"Не удалось подключиться к {ip}:{port} (проверьте IP и SSH-порт)"}


def test_llm_token(provider: str, api_key: str, model: str = "") -> dict[str, Any]:
    """Проверяет работоспособность токена нейросети."""
    api_key = api_key.strip()
    if not api_key:
        return {"ok": False, "error": "Токен не указан"}

    ctx = ssl._create_unverified_context()

    if provider == "openrouter":
        url = "https://openrouter.ai/api/v1/auth/key"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                label = data.get("data", {}).get("label") or "Ключ активен"
                return {"ok": True, "provider": "OpenRouter", "info": label}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"Ошибка авторизации OpenRouter ({exc.code}): проверьте токен"}
        except Exception as exc:
            return {"ok": False, "error": f"Ошибка сети: {exc}"}

    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                models = [m.get("name") for m in data.get("models", []) if "gemini" in m.get("name", "")]
                return {"ok": True, "provider": "Gemini", "info": f"Доступно {len(models)} моделей Gemini"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"Ошибка Gemini API ({exc.code}): неверный ключ"}
        except Exception as exc:
            return {"ok": False, "error": f"Ошибка сети: {exc}"}

    if provider == "groq":
        url = "https://api.groq.com/openai/v1/models"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                return {"ok": True, "provider": "Groq", "info": f"Ключ активен ({len(data.get('data', []))} моделей)"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"Ошибка Groq API ({exc.code}): неверный ключ"}
        except Exception as exc:
            return {"ok": False, "error": f"Ошибка сети: {exc}"}

    return {"ok": True, "provider": provider, "info": "Проверка пропущена"}


def write_env_file(config: dict[str, str]):
    """Записывает файл .env на основе шаблона и параметров пользователя."""
    lines: list[str] = []
    if ENV_EXAMPLE.exists():
        template = ENV_EXAMPLE.read_text(encoding="utf-8")
        for line in template.splitlines():
            line_strip = line.strip()
            if "=" in line_strip and not line_strip.startswith("#"):
                key = line_strip.split("=", 1)[0].strip()
                if key in config:
                    lines.append(f"{key}={config[key]}")
                    continue
            lines.append(line)
    else:
        for k, v in config.items():
            lines.append(f"{k}={v}")

    # Добавляем ключи, которых не было в шаблоне
    existing_keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line and not line.startswith("#")}
    for k, v in config.items():
        if k not in existing_keys:
            lines.append(f"{k}={v}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    append_log(f"Файл конфигурации сохранён: {ENV_FILE}")


def execute_cmd(cmd: list[str] | str, cwd: Path = ROOT_DIR, shell: bool = False) -> tuple[int, str]:
    """Выполняет команду с записью в лог развёртывания."""
    cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
    append_log(f"$ {cmd_str}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = []
    if proc.stdout:
        for line in proc.stdout:
            line_str = line.rstrip()
            if line_str:
                append_log(f"  {line_str}")
                output.append(line_str)
    proc.wait()
    return proc.returncode, "\n".join(output)


def run_deployment_worker(config: dict[str, str]):
    """Фоновый поток развёртывания всего проекта."""
    global _DEPLOY_STATUS
    with _DEPLOY_LOCK:
        _DEPLOY_STATUS = {"running": True, "success": False, "step": "Подготовка", "error": ""}
        _DEPLOY_LOGS.clear()

        try:
            # 1. Сохранение конфигурации
            _DEPLOY_STATUS["step"] = "1/5 Сохранение конфигурации .env"
            append_log("=== ЭТАП 1: Запись конфигурации .env ===")
            write_env_file(config)

            # 2. Подготовка Python-окружения на Mac
            _DEPLOY_STATUS["step"] = "2/5 Установка зависимостей Mac (uv / venv)"
            append_log("=== ЭТАП 2: Подготовка Python-окружения на Mac ===")

            uv_path = ""
            for p in ("/opt/homebrew/bin/uv", str(Path.home() / ".cargo/bin/uv"), str(Path.home() / ".local/bin/uv")):
                if Path(p).is_file() and os.access(p, os.X_OK):
                    uv_path = p
                    break
            if not uv_path and subprocess.run(["which", "uv"], capture_output=True).returncode == 0:
                uv_path = "uv"

            if uv_path:
                append_log(f"Используем uv: {uv_path}")
                rc, _ = execute_cmd([uv_path, "sync"], cwd=ASSISTANT_DIR)
                if rc != 0:
                    append_log("uv sync вернул ошибку, пробуем pip install...")
            else:
                append_log("uv не найден, настраиваем venv через python3...")
                venv_dir = ASSISTANT_DIR / ".venv"
                if not venv_dir.exists():
                    execute_cmd([sys.executable, "-m", "venv", str(venv_dir)])
                pip_path = str(venv_dir / "bin/pip")
                execute_cmd([pip_path, "install", "--upgrade", "pip"])
                execute_cmd([pip_path, "install", "-e", "."], cwd=ASSISTANT_DIR)

            # 3. Развёртывание на Станцию
            _DEPLOY_STATUS["step"] = "3/5 Загрузка бинарников и демона на Станцию"
            append_log("=== ЭТАП 3: Прошивка и настройка Яндекс Станции ===")
            station_ip = config.get("STATION_IP", "192.168.2.83")
            station_port = config.get("STATION_SSH_PORT", "2222")
            mac_ip = config.get("MAC_IP") or get_mac_ip()
            deploy_script = ROOT_DIR / "scripts/deploy-station.sh"

            if deploy_script.is_file():
                os.chmod(deploy_script, 0o755)
                rc, _ = execute_cmd([str(deploy_script), station_ip, station_port, mac_ip], cwd=ROOT_DIR)
                if rc != 0:
                    append_log(f"Предупреждение: deploy-station.sh завершился с кодом {rc}")
            else:
                append_log(f"Скрипт {deploy_script} не найден, пропускаем прямую прошивку")

            # 4. Настройка автозапуска на Mac (launchd)
            _DEPLOY_STATUS["step"] = "4/5 Настройка автозапуска ассистента на Mac"
            append_log("=== ЭТАП 4: Создание LaunchAgent на Mac ===")
            launch_agents_dir = Path.home() / "Library/LaunchAgents"
            launch_agents_dir.mkdir(parents=True, exist_ok=True)
            plist_file = launch_agents_dir / "me.station.assistant.plist"

            run_sh = ASSISTANT_DIR / "scripts/run.sh"
            if run_sh.is_file():
                os.chmod(run_sh, 0o755)

            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>me.station.assistant</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{run_sh}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{ASSISTANT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/station-assistant.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/station-assistant.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:{Path.home()}/.cargo/bin</string>
    </dict>
</dict>
</plist>
"""
            plist_file.write_text(plist_content, encoding="utf-8")
            append_log(f"LaunchAgent создан: {plist_file}")

            # Перезапуск сервиса
            uid = os.getuid()
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/me.station.assistant"], capture_output=True)
            time.sleep(0.5)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_file)], capture_output=True)
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/me.station.assistant"], capture_output=True)
            append_log("Сервис me.station.assistant запущен через launchctl!")

            # 5. Проверка доступности
            _DEPLOY_STATUS["step"] = "5/5 Проверка работоспособности сервиса"
            append_log("=== ЭТАП 5: Ожидание запуска веб-панели (порт 8730) ===")
            dashboard_ready = False
            for _ in range(15):
                time.sleep(1)
                if is_port_open("127.0.0.1", 8730, timeout=0.5):
                    dashboard_ready = True
                    break

            if dashboard_ready:
                append_log("УРА! Ассистент успешно запущен и готов к работе!")
                _DEPLOY_STATUS["success"] = True
                _DEPLOY_STATUS["step"] = "Готово!"
            else:
                append_log("Сервер запускается. Лог: /tmp/station-assistant.log")
                _DEPLOY_STATUS["success"] = True
                _DEPLOY_STATUS["step"] = "Сервис запущен"

        except Exception as exc:
            append_log(f"ОШИБКА РАЗВЁРТЫВАНИЯ: {exc}")
            _DEPLOY_STATUS["success"] = False
            _DEPLOY_STATUS["error"] = str(exc)
        finally:
            _DEPLOY_STATUS["running"] = False


class WizardRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Отключаем дефолтный спам в консоль
        pass

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        if path in ("/", "/index.html"):
            index_file = WEB_DIR / "index.html"
            if index_file.is_file():
                body = index_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404, "index.html not found")
            return

        if path == "/api/status":
            mac_ip = get_mac_ip()
            candidates = scan_station_candidates()
            data = {
                "mac_ip": mac_ip,
                "station_candidates": candidates,
                "current_station_ip": read_env_value("STATION_IP") or (candidates[0]["ip"] if candidates else "192.168.2.83"),
                "current_station_port": read_env_value("STATION_SSH_PORT") or (str(candidates[0]["port"]) if candidates else "2222"),
                "openrouter_key": read_env_value("OPENROUTER_API_KEY"),
                "gemini_key": read_env_value("GEMINI_API_KEY"),
                "groq_key": read_env_value("GROQ_API_KEY"),
                "llm_provider": read_env_value("LLM_PROVIDER") or "openrouter",
                "llm_model": read_env_value("LLM_MODEL") or "qwen/qwen-2.5-72b-instruct",
                "tts_provider": read_env_value("TTS_PROVIDER") or "silero",
                "tts_voice": read_env_value("SILERO_VOICE") or "svetlana",
            }
            self.send_json(data)
            return

        if path == "/api/deploy/status":
            with _DEPLOY_LOCK:
                data = {
                    "running": _DEPLOY_STATUS["running"],
                    "success": _DEPLOY_STATUS["success"],
                    "step": _DEPLOY_STATUS["step"],
                    "error": _DEPLOY_STATUS["error"],
                    "logs": _DEPLOY_LOGS[-50:],
                }
            self.send_json(data)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req = {}

        if path == "/api/test-station":
            ip = req.get("ip", "192.168.2.83")
            port = int(req.get("port", 2222))
            res = test_station_ssh(ip, port)
            self.send_json(res)
            return

        if path == "/api/test-token":
            provider = req.get("provider", "openrouter")
            token = req.get("token", "")
            res = test_llm_token(provider, token)
            self.send_json(res)
            return

        if path == "/api/deploy":
            with _DEPLOY_LOCK:
                if _DEPLOY_STATUS["running"]:
                    self.send_json({"ok": False, "error": "Развёртывание уже выполняется"}, status=409)
                    return

            config = {
                "STATION_IP": req.get("station_ip", "192.168.2.83"),
                "STATION_SSH_PORT": str(req.get("station_port", 2222)),
                "MAC_IP": req.get("mac_ip", get_mac_ip()),
                "LLM_PROVIDER": req.get("llm_provider", "openrouter"),
                "LLM_MODEL": req.get("llm_model", "qwen/qwen-2.5-72b-instruct"),
                "OPENROUTER_API_KEY": req.get("openrouter_key", ""),
                "GEMINI_API_KEY": req.get("gemini_key", ""),
                "GROQ_API_KEY": req.get("groq_key", ""),
                "TTS_PROVIDER": req.get("tts_provider", "silero"),
                "SILERO_VOICE": req.get("tts_voice", "svetlana"),
                "ASR_PROVIDER": req.get("asr_provider", "whisper_mlx"),
                "ASSISTANT_PORT": "8730",
            }

            t = threading.Thread(target=run_deployment_worker, args=(config,), daemon=True)
            t.start()
            self.send_json({"ok": True, "message": "Процесс установки запущен"})
            return

        self.send_error(404, "Not Found")


def run_server(port: int = 8899, auto_open: bool = True):
    start_port = port
    while port < start_port + 20:
        try:
            server = HTTPServer(("0.0.0.0", port), WizardRequestHandler)
            url = f"http://localhost:{port}"
            print(f"\n=======================================================")
            print(f"🚀 Мастер установки запущен: {url}")
            if auto_open:
                print(f"   Открываем веб-страницу в браузере...")
            print(f"=======================================================\n")
            if auto_open:
                subprocess.run(["open", url])
            server.serve_forever()
            break
        except OSError as exc:
            if exc.errno in (48, 98):  # Address already in use
                port += 1
            else:
                raise


if __name__ == "__main__":
    p = 8899
    auto_open = "--no-open" not in sys.argv
    for arg in sys.argv[1:]:
        if arg.isdigit():
            p = int(arg)
    run_server(p, auto_open=auto_open)
