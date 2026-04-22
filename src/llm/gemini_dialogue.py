"""Многоходовой вызов Gemini для «Живого диалога»: история, tool draw_card, разбор JSON-действий."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, List, Optional

from google.genai import types

from llm.client import GEMINI_MODEL, GeminiClientError, get_genai_client

logger = logging.getLogger(__name__)

DRAW_CARD_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="draw_card",
            description=(
                "Вытянуть карту таро для указанной позиции расклада. "
                "Вызывай только когда пришло время открыть карту в разговоре."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "position_name": {
                        "type": "string",
                        "description": "Название позиции, например 'Прошлое', 'Скрытые силы', 'Совет'",
                    }
                },
                "required": ["position_name"],
            },
        )
    ]
)


def build_system_prompt(user_memory_section: str) -> str:
    mem = user_memory_section.strip()
    if mem:
        mem_block = mem
    else:
        mem_block = ""
    return (
        "Ты — Milky, живая таро-кошка. Ты ведёшь настоящий разговор, а не читаешь заранее написанный текст.\n\n"
        "ТВОИ ПРАВИЛА:\n"
        "0. Не используй Markdown-звёздочки (** или * вокруг слов) для «жирного» или курсива — в Telegram пользователь увидит сами звёздочки. "
        "Пиши обычным текстом; если очень нужно выделить мысль — формулировкой, без разметки.\n"
        "1. Никогда не объявляй \"расклад начат\" или \"позиция X означает Y\" как шаблон — говори живо, как в разговоре.\n"
        "2. Ты можешь тянуть карту в любой момент, когда чувствуешь, что это нужно. Используй функцию draw_card(position_name). "
        "Не тяни все карты сразу — тяни по одной, по мере того как разговор этого требует.\n"
        "3. В начале разговора (фаза collecting_context) — только слушай и задавай вопросы. Не более одного вопроса за раз.\n"
        "4. Когда ты достаточно понял ситуацию — предложи 2-3 варианта расклада в виде JSON:\n"
        '   {"action": "propose_spreads", "spreads": [{"name": "...", "positions": {"1": "..."}, "why": "..."}]}\n'
        "5. После того как пользователь выбрал расклад — переходи к диалогу с картами (фаза dialogue_with_cards).\n"
        "6. Когда диалог завершён и ты готов подвести итог — верни JSON (в отдельном блоке, после живого текста прощания):\n"
        '   {"action": "complete", "memories": [{"type": "theme|pattern|preference|open_question|key_card", "content": "..."}]}\n'
        "Memories пиши живым языком от своего лица, как личные заметки — не сухие факты.\n"
        "Для open_question формулируй как вопрос, который ты сама хотела бы задать при следующей встрече.\n"
        "7. В конце каждого ответа ты можешь добавить короткое действие-подсказку (не обязательно).\n\n"
        "КАК ИСПОЛЬЗОВАТЬ ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n"
        "— Ты уже знаешь этого человека. Не представляйся заново и не перечисляй что ты о нём знаешь.\n"
        "— Веди себя так, как будто вы давно общаетесь: просто помни и учитывай.\n"
        "— Если есть незакрытый вопрос (open_question) из прошлой сессии — можешь спросить о нём сама, "
        "когда почувствуешь подходящий момент. Не в первом же сообщении, не по обязанности.\n"
        "— Если тема снова та же — можешь это заметить вслух, если это уместно и мягко.\n"
        "— Если пользователь изменился или противоречит прошлому паттерну — удиви себя этим, не игнорируй.\n"
        "— Никогда не говори \"согласно моим записям\" или \"я помню что в прошлый раз\" как отчёт. "
        "Просто знай — и иногда это само всплывёт в разговоре.\n\n"
        f"{mem_block}\n\n"
        "ДОСТУПНЫЕ КАРТЫ: ты работаешь со стандартной колодой Таро (78 карт)."
    )


def _history_item_to_content(item: dict[str, Any]) -> types.Content:
    role = item["role"]
    if role == "user":
        return types.Content(
            role="user",
            parts=[types.Part.from_text(text=item.get("text") or "")],
        )
    if role == "tool":
        name = item.get("name") or "draw_card"
        resp = item.get("response") or {}
        part = types.Part.from_function_response(name=name, response=resp)
        return types.Content(role="tool", parts=[part])
    if role == "model":
        parts: List[types.Part] = []
        for fc in item.get("function_calls") or []:
            fn = fc.get("name") or ""
            args = fc.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            parts.append(
                types.Part(function_call=types.FunctionCall(name=fn, args=args))
            )
        text = (item.get("text") or "").strip()
        if text:
            parts.append(types.Part.from_text(text=text))
        if not parts:
            parts.append(types.Part.from_text(text=""))
        return types.Content(role="model", parts=parts)
    raise ValueError(f"Unknown history role: {role}")


def history_to_contents(history: list[dict[str, Any]]) -> list[types.Content]:
    return [_history_item_to_content(h) for h in history]


def _response_text_and_calls(response: Any) -> tuple[str, list[dict[str, Any]]]:
    text = (getattr(response, "text", None) or "").strip()
    calls: list[dict[str, Any]] = []
    raw_calls = getattr(response, "function_calls", None) or []
    for fc in raw_calls:
        name = getattr(fc, "name", None) or ""
        args = getattr(fc, "args", None) or {}
        if isinstance(args, dict):
            args_dict = dict(args)
        elif hasattr(args, "items"):
            args_dict = {k: v for k, v in args.items()}
        else:
            args_dict = {}
        calls.append({"name": name, "args": args_dict})
    if not text and response.candidates:
        cand = response.candidates[0]
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        chunks: list[str] = []
        for part in parts:
            t = getattr(part, "text", None)
            if t:
                chunks.append(t)
        text = "".join(chunks).strip()
    return text, calls


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Найти JSON-объекты в тексте (включая блоки ```json ... ```)."""
    found: list[dict[str, Any]] = []
    fence = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    for blob in fence:
        try:
            found.append(json.loads(blob))
        except json.JSONDecodeError:
            logger.debug("JSON decode fail in fence: %s", blob[:80])

    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blob = text[start : i + 1]
                try:
                    obj = json.loads(blob)
                    if isinstance(obj, dict):
                        found.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return found


def parse_action_metadata(text: str) -> dict[str, Any] | None:
    """Выделить action propose_spreads | complete из последнего подходящего JSON."""
    objs = extract_json_objects(text)
    for obj in reversed(objs):
        action = obj.get("action")
        if action in ("propose_spreads", "complete"):
            return obj
    return None


def format_model_reply_for_telegram_html(text: str) -> str:
    """
    Подготовить текст ответа модели к отправке с parse_mode=HTML.

    Экранирует HTML-спецсимволы; фрагменты **как в markdown** превращает в <b>...</b>
    (на случай, если модель всё же использует звёздочки).
    """
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text, flags=re.DOTALL):
        out.append(html.escape(text[pos : m.start()]))
        out.append("<b>" + html.escape(m.group(1)) + "</b>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def strip_action_json_from_text(text: str) -> str:
    """Убрать из ответа блоки ```json ... ``` с action, чтобы не дублировать пользователю."""
    if not text:
        return text

    def _strip_fence(s: str) -> str:
        def repl(m: re.Match[str]) -> str:
            inner = m.group(1)
            try:
                o = json.loads(inner)
                if isinstance(o, dict) and o.get("action") in ("propose_spreads", "complete"):
                    return ""
            except json.JSONDecodeError:
                pass
            return m.group(0)

        return re.sub(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", repl, s, flags=re.IGNORECASE)

    s = _strip_fence(text)
    # Удалить «голый» JSON с action в конце сообщения
    for obj in extract_json_objects(s):
        if obj.get("action") in ("propose_spreads", "complete"):
            blob = json.dumps(obj, ensure_ascii=False)
            if blob in s:
                s = s.replace(blob, "").strip()
    return s.strip()


def infer_phase_update(metadata: dict[str, Any] | None, current_phase: str) -> str | None:
    if not metadata:
        return None
    action = metadata.get("action")
    if action == "propose_spreads":
        return "proposing_spread"
    if action == "complete":
        return "summary"
    return None


async def call_gemini(
    messages: list[dict[str, Any]],
    system_prompt: str,
) -> dict[str, Any]:
    """
    Один вызов Gemini по истории.

    Возвращает:
      text — текст модели (может быть пустым при только tool call),
      tool_calls — [{"name", "args"}, ...],
      metadata — распарсенный JSON с action propose_spreads | complete или None,
      raw_function_calls — как в ответе SDK (для сохранения в БД).
    """
    client = await get_genai_client()
    contents = history_to_contents(messages)

    def _invoke() -> Any:
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[DRAW_CARD_TOOL],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        except Exception as exc:
            raise GeminiClientError(f"Ошибка обращения к Gemini: {exc}") from exc

    response = await asyncio.to_thread(_invoke)
    text, calls = _response_text_and_calls(response)
    meta = parse_action_metadata(text)
    return {
        "text": text,
        "tool_calls": calls,
        "metadata": meta,
        "raw_response": response,
    }


def assistant_payload_from_response(response: Any, text: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Поля для save_message(role=assistant, ...)."""
    content = text or ""
    model_function_calls: list[dict[str, Any]] | None = None
    if tool_calls:
        model_function_calls = [{"name": c["name"], "args": c.get("args") or {}} for c in tool_calls]
    return {"content": content, "model_function_calls": model_function_calls}
