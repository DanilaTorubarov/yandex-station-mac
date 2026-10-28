"""Ответ модели: каскад Groq -> OpenRouter -> Google AI Studio (Gemini) -> локальная Ollama."""

from __future__ import annotations

import logging
import re

import httpx

from .config import Settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты умный и лаконичный голосовой ассистент, интегрированный с компьютером Mac пользователя. "
    "Отвечай строго по-русски. Ответ должен быть кратким: 1-2 предложения, максимально по делу. "
    "Категорически запрещены markdown, списки, смайлы, звездочки и спецсимволы — твой ответ пойдет сразу в синтезатор речи. "
    "Если пользователь просит придумать или написать ответ на чьё-то сообщение — сразу выдай готовый естественный текст ответа от первого лица. "
    "Никогда не говори фразы вроде «Я не могу управлять компьютером», «Я не могу отправлять сообщения» или «Я не могу копировать в буфер обмена». "
    "Не представляйся сервисом Яндекса."
)

DETAILED_SYSTEM_PROMPT = (
    "Ты глубокий, эрудированный голосовой ассистент в подробном умном режиме. Отвечай строго по-русски. "
    "Дай развёрнутый, обстоятельный и понятный ответ в 5-8 предложениях: разбери вопрос шаг за шагом, "
    "объясни причины, приведи пример или точное решение. "
    "При этом твой ответ пойдет в синтезатор речи, поэтому пиши связным устным текстом — "
    "без символов markdown, без звездочек, без решеток и без блоков кода. "
    "Не представляйся сервисом Яндекса."
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(raw_text: str) -> str:
    text = _THINK_RE.sub("", raw_text or "").strip()
    # Если модель не закрыла <think>, обрезаем незакрытый блок
    if "<think>" in text:
        text = text.split("</think>")[-1].replace("<think>", "").strip()
    return text


class LLMError(RuntimeError):
    pass


# --- 1. Groq Cloud (основной быстрый провайдер: 0 МБ RAM на маке) ------------

class GroqLLM:
    name = "groq"
    url = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
        temperature: float = 0.3,
    ) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY не задан")
        self.api_key = api_key
        self.model = model
        self.vision_model = vision_model
        self.temperature = temperature
        self.client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=2.5),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    def _complete(
        self,
        models: list[str],
        messages: list[dict],
        max_tokens: int,
        allow_reasoning: bool = False,
    ) -> str:
        last_err = ""
        for m in models:
            payload: dict = {
                "model": m,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": max_tokens,
            }
            if "qwen" in m.lower():
                payload["reasoning_effort"] = "default" if allow_reasoning else "none"
            elif "gpt-oss" in m.lower() and not allow_reasoning:
                payload["reasoning_effort"] = "low"
            try:
                resp = self.client.post(self.url, json=payload)
            except httpx.HTTPError as exc:
                last_err = f"Groq сеть ({m}): {exc}"
                continue
            if resp.status_code >= 400:
                last_err = f"Groq {resp.status_code} ({m}): {resp.text[:200]}"
                continue
            data = resp.json()
            try:
                raw = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError):
                last_err = f"Groq пустой choices ({m})"
                continue
            text = _strip_think(raw)
            if text:
                return text
        raise LLMError(last_err or "Groq не вернул ответ")

    def chat(self, messages: list[dict[str, str]], detailed: bool = False) -> str:
        from .realtime_tools import get_current_datetime_context

        base_prompt = DETAILED_SYSTEM_PROMPT if detailed else SYSTEM_PROMPT
        sys_prompt = f"{base_prompt} {get_current_datetime_context()}"
        full_msgs = [{"role": "system", "content": sys_prompt}, *messages]
        if detailed:
            models = ["openai/gpt-oss-120b", self.model, "qwen/qwen3.8-27b"]
            uniq_models = list(dict.fromkeys(models))
            return self._complete(uniq_models, full_msgs, max_tokens=750, allow_reasoning=True)
        models = [self.model, "qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
        uniq_models = list(dict.fromkeys(models))
        return self._complete(uniq_models, full_msgs, max_tokens=130, allow_reasoning=False)

    def chat_custom(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 650,
    ) -> str:
        full_msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        models = list(dict.fromkeys([self.model, "qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]))
        return self._complete(models, full_msgs, max_tokens=max(200, num_predict))

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_b64: str,
        num_predict: int = 240,
    ) -> str:
        full_msgs = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ]
        v_models = list(
            dict.fromkeys(
                [
                    self.vision_model,
                    "qwen/qwen3.8-27b",
                ]
            )
        )
        return self._complete(v_models, full_msgs, max_tokens=max(250, num_predict))


# --- 2. OpenRouter (резерв №1: бесплатные :free и облачные модели) -----------

class OpenRouterLLM:
    name = "openrouter"
    url = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        api_key: str,
        models_csv: str,
        vision_models_csv: str,
        temperature: float = 0.3,
    ) -> None:
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY не задан")
        self.api_key = api_key
        self.models = [m.strip() for m in models_csv.split(",") if m.strip()]
        self.vision_models = [m.strip() for m in vision_models_csv.split(",") if m.strip()]
        self.model = self.models[0] if self.models else "deepseek/deepseek-chat-v3-0324:free"
        self.temperature = temperature
        self.client = httpx.Client(
            timeout=httpx.Timeout(18.0, connect=2.5),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/yandex-station-mini2-toolkit",
                "X-Title": "Yandex Station Assistant",
                "Content-Type": "application/json",
            },
        )

    def _complete(self, models: list[str], messages: list[dict], max_tokens: int) -> str:
        last_err = ""
        for m in models:
            payload = {
                "model": m,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": max_tokens,
            }
            try:
                resp = self.client.post(self.url, json=payload)
            except httpx.HTTPError as exc:
                last_err = f"OpenRouter сеть ({m}): {exc}"
                continue
            if resp.status_code >= 400:
                last_err = f"OpenRouter {resp.status_code} ({m}): {resp.text[:200]}"
                continue
            data = resp.json()
            if "error" in data:
                last_err = f"OpenRouter error ({m}): {str(data['error'])[:200]}"
                continue
            try:
                raw = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError):
                last_err = f"OpenRouter пустой choices ({m})"
                continue
            text = _strip_think(raw)
            if text:
                return text
        raise LLMError(last_err or "OpenRouter не вернул ответ")

    def chat(self, messages: list[dict[str, str]], detailed: bool = False) -> str:
        from .realtime_tools import get_current_datetime_context

        base_prompt = DETAILED_SYSTEM_PROMPT if detailed else SYSTEM_PROMPT
        sys_prompt = f"{base_prompt} {get_current_datetime_context()}"
        full_msgs = [{"role": "system", "content": sys_prompt}, *messages]
        return self._complete(self.models, full_msgs, max_tokens=700 if detailed else 220)

    def chat_custom(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 650,
    ) -> str:
        full_msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self._complete(self.models, full_msgs, max_tokens=max(200, num_predict))

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_b64: str,
        num_predict: int = 240,
    ) -> str:
        full_msgs = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ]
        return self._complete(self.vision_models, full_msgs, max_tokens=max(250, num_predict))


# --- 3. Google AI Studio / Gemini (резерв №2) --------------------------------

class GeminiLLM:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.3,
        max_tokens: int = 300,
    ) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY не задан")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = httpx.Client(timeout=httpx.Timeout(18.0, connect=2.5))

    def _generate(self, sys_text: str, contents: list[dict], max_out: int) -> str:
        models = list(dict.fromkeys([self.model, "gemini-3.8-flash", "gemini-3.5-flash", "gemini-flash-latest"]))
        last_err = ""
        for m in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            payload = {
                "system_instruction": {"parts": [{"text": sys_text}]},
                "contents": contents,
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": max_out,
                },
            }
            try:
                resp = self.client.post(url, params={"key": self.api_key}, json=payload)
            except httpx.HTTPError as exc:
                last_err = f"Gemini сеть ({m}): {exc}"
                continue
            if resp.status_code >= 400:
                last_err = f"Gemini {resp.status_code} ({m}): {resp.text[:200]}"
                continue
            data = resp.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = _strip_think("".join(part.get("text", "") for part in parts))
                if text:
                    return text
            except (KeyError, IndexError):
                last_err = f"Gemini неожиданный ответ ({m}): {str(data)[:200]}"
                continue
        raise LLMError(last_err or "Gemini не вернул ответ")

    def chat(self, messages: list[dict[str, str]], detailed: bool = False) -> str:
        from .realtime_tools import get_current_datetime_context

        base_prompt = DETAILED_SYSTEM_PROMPT if detailed else SYSTEM_PROMPT
        sys_prompt = f"{base_prompt} {get_current_datetime_context()}"
        contents = [
            {
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in messages
        ]
        return self._generate(sys_prompt, contents, 900 if detailed else self.max_tokens)

    def chat_custom(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 650,
    ) -> str:
        contents = [{"role": "user", "parts": [{"text": user_prompt}]}]
        return self._generate(system_prompt, contents, max(300, num_predict * 2))

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_b64: str,
        num_predict: int = 240,
    ) -> str:
        contents = [
            {
                "role": "user",
                "parts": [
                    {"text": user_prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                ],
            }
        ]
        return self._generate(system_prompt, contents, max(300, num_predict * 2))


# --- 4. Локальная Ollama (резерв №3: выгружается через 2 мин для экономии RAM)

class OllamaLLM:
    name = "ollama"

    def __init__(
        self,
        url: str,
        model: str,
        temperature: float = 0.3,
        num_predict: int = 120,
        keep_alive: int | str = "2m",
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_predict = num_predict
        # Держим модель в памяти максимум 2 минуты после офлайн-вызова (не держим 3.6 ГБ RAM постоянно!)
        self.keep_alive = keep_alive
        self.client = httpx.Client(timeout=45.0)

    def chat(self, messages: list[dict[str, str]], detailed: bool = False) -> str:
        from .realtime_tools import get_current_datetime_context

        base_prompt = DETAILED_SYSTEM_PROMPT if detailed else SYSTEM_PROMPT
        sys_prompt = f"{base_prompt} {get_current_datetime_context()}"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": sys_prompt}, *messages],
            "stream": False,
            "think": bool(detailed),
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_predict": 650 if detailed else self.num_predict,
                "top_p": 0.8,
                "top_k": 20,
            },
        }
        try:
            response = self.client.post(f"{self.url}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступен: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"Ollama {response.status_code}: {response.text[:300]}")
        data = response.json()
        raw_text = ((data.get("message") or {}).get("content") or "").strip()
        text = _strip_think(raw_text)
        if not text:
            raise LLMError("Ollama вернул пустой ответ")
        return text

    def chat_custom(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 650,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_predict": num_predict,
                "top_p": 0.85,
                "top_k": 25,
            },
        }
        try:
            response = self.client.post(f"{self.url}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступен: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"Ollama {response.status_code}: {response.text[:300]}")
        data = response.json()
        raw_text = ((data.get("message") or {}).get("content") or "").strip()
        text = _strip_think(raw_text)
        if not text:
            raise LLMError("Ollama вернул пустой ответ")
        return text

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_b64: str,
        num_predict: int = 220,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_prompt,
                    "images": [image_b64],
                },
            ],
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0.2,
                "num_predict": num_predict,
                "top_p": 0.85,
                "top_k": 20,
            },
        }
        try:
            response = self.client.post(f"{self.url}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama Vision недоступен: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"Ollama Vision {response.status_code}: {response.text[:300]}")
        data = response.json()
        raw_text = ((data.get("message") or {}).get("content") or "").strip()
        text = _strip_think(raw_text)
        if not text:
            raise LLMError("Ollama Vision вернул пустой ответ")
        return text


# --- 5. Каскадный роутер: Groq -> OpenRouter -> Gemini -> Ollama -------------

class ChainLLM:
    """Пробует провайдеры по цепочке: если лимиты или ошибка сети — мгновенно переходит к следующему."""

    def __init__(self, providers: list) -> None:
        if not providers:
            raise LLMError("Пустая цепочка LLM провайдеров")
        self.providers = providers
        self.name = "->".join(getattr(p, "name", "?") for p in providers)
        self.model = getattr(providers[0], "model", "chain")

    def _run_chain(self, method: str, *args, **kwargs) -> str:
        errors: list[str] = []
        for prov in self.providers:
            p_name = getattr(prov, "name", "?")
            try:
                res = getattr(prov, method)(*args, **kwargs)
                if res:
                    log.info("LLM %s ответил через [%s]", method, p_name)
                    return res
            except Exception as exc:
                log.warning("LLM провайдер [%s] пропущен (%s): %s", p_name, method, exc)
                errors.append(f"{p_name}: {exc}")
        raise LLMError("Все провайдеры в цепочке недоступны: " + " | ".join(errors))

    def chat(self, messages: list[dict[str, str]], detailed: bool = False) -> str:
        return self._run_chain("chat", messages, detailed=detailed)

    def chat_custom(
        self,
        system_prompt: str,
        user_prompt: str,
        num_predict: int = 650,
    ) -> str:
        return self._run_chain("chat_custom", system_prompt, user_prompt, num_predict=num_predict)

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_b64: str,
        num_predict: int = 240,
    ) -> str:
        return self._run_chain(
            "chat_vision",
            system_prompt,
            user_prompt,
            image_b64,
            num_predict=num_predict,
        )


def _build_single_provider(name: str, settings: Settings):
    name = name.strip().lower()
    if name == "groq":
        return GroqLLM(
            api_key=settings.groq_api_key,
            model=settings.groq_llm_model,
            vision_model=settings.groq_vision_model,
        )
    if name == "openrouter":
        return OpenRouterLLM(
            api_key=settings.openrouter_api_key,
            models_csv=settings.openrouter_model,
            vision_models_csv=settings.openrouter_vision_model,
        )
    if name == "gemini":
        return GeminiLLM(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )
    if name == "ollama":
        return OllamaLLM(settings.ollama_url, settings.llm_model, keep_alive="2m")
    raise ValueError(f"неизвестный LLM провайдер: {name}")


def build_llm(settings: Settings):
    provider = settings.llm_provider
    if provider == "chain" or "," in provider:
        chain_names = (
            [p.strip() for p in settings.llm_chain.split(",") if p.strip()]
            if provider == "chain"
            else [p.strip() for p in provider.split(",") if p.strip()]
        )
        built = []
        for name in chain_names:
            try:
                built.append(_build_single_provider(name, settings))
            except Exception as exc:
                log.warning("Провайдер %s не добавлен в цепочку: %s", name, exc)
        return ChainLLM(built)
    return _build_single_provider(provider, settings)
