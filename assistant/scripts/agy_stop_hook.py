#!/usr/bin/env python3
"""
Lifecycle Stop-хук для Antigravity: когда агент завершает выполнение задачи,
отправляет голосовое уведомление на Яндекс Станцию через POST http://127.0.0.1:8730/say.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

LOCK_FILE = "/tmp/agy_stop_hook.ts"


def _should_announce() -> bool:
    """Защита от дублирования (если хук прописан и глобально, и в проекте)."""
    now = time.time()
    try:
        if os.path.exists(LOCK_FILE):
            last = float(open(LOCK_FILE, "r", encoding="utf-8").read().strip() or "0")
            if now - last < 4.0:
                return False
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception:
        pass
    return True


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    reason = str(payload.get("terminationReason") or "NO_TOOL_CALL")
    fully_idle = payload.get("fullyIdle", True)

    # Озвучиваем только когда агент действительно закончил работу (не отменён пользователем вручную)
    if reason != "USER_CANCELED" and fully_idle is not False and _should_announce():
        if reason == "ERROR" or payload.get("error"):
            text = "Внимание, в Антигравити произошла ошибка при выполнении задачи."
        else:
            text = "Антигравити закончила работу."

        try:
            body = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:8730/say",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.5).read()
        except Exception:
            pass

    # Возвращаем штатный ответ Stop-хука Antigravity, чтобы не блокировать завершение хода
    sys.stdout.write(json.dumps({"decision": "stop"}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
