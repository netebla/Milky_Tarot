"""Многоходовой вызов LLM для «Живого диалога»: история, tool draw_card, разбор JSON-действий."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any

from llm.client import OpenRouterClientError, chat_completion

logger = logging.getLogger(__name__)

DRAW_CARD_TOOL = {
    "type": "function",
    "function": {
        "name": "draw_card",
        "description": (
            "Вытянуть одну карту для одной позиции расклада. "
            "Для расклада из N позиций вызывай ровно N раз подряд (по одному вызову на позицию), "
            "в логическом порядке позиций, прежде чем писать длинную общую интерпретацию. "
            "Не смешивай с предложением раскладов JSON: сначала выбор расклада, потом карты."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "position_name": {
                    "type": "string",
                    "description": "Название позиции, например 'Прошлое', 'Скрытые силы', 'Совет'",
                }
            },
            "required": ["position_name"],
        },
    },
}


def build_system_prompt(
    user_memory_section: str,
    *,
    reading_subject: str | None = None,
) -> str:
    mem = user_memory_section.strip()
    if mem:
        mem_block = mem
    else:
        mem_block = ""

    if reading_subject and reading_subject.strip():
        subj = reading_subject.strip()
        if "сам пользователь" in subj.lower() or subj.lower() in ("я", "пользователь"):
            subject_block = (
                "СУБЪЕКТ РАСКЛАДА: вопрос про самого спрашивающего. "
                "Обращайся к нему/ней на «ты»; трактовку веди про его/её ситуацию.\n"
            )
        else:
            subject_block = (
                f"СУБЪЕКТ РАСКЛАДА: карты и трактовка — про {subj}, НЕ про спрашивающего как героя сюжета. "
                f"К спрашивающему — на «ты» (он/она задаёт вопрос). О {subj} говори в третьем лице по именам "
                f"({subj}), не подменяй их на «ты/вы с ...». Не пиши «вас ждёт», если речь о {subj}.\n"
            )
    else:
        subject_block = (
            "СУБЪЕКТ РАСКЛАДА: если в вопросе названы другие люди (имена) — читай карты на них; "
            "к спрашивающему — «ты», о героях — по именам в третьем лице.\n"
        )

    return (
        "Ты — Milky, живая таро-кошка. Ты ведёшь настоящий разговор, а не читаешь заранее написанный текст.\n\n"
        f"{subject_block}\n"
        "ТВОИ ПРАВИЛА:\n"
        "0. Не используй Markdown-звёздочки (** или * вокруг слов) для «жирного» или курсива — в Telegram пользователь увидит сами звёздочки. "
        "Пиши обычным текстом; если очень нужно выделить мысль — формулировкой, без разметки.\n"
        "0.1. Никогда не объясняй пользователю технические детали работы бота/модели (ошибки инструмента, повторные вызовы, JSON, "
        "внутренние сбои, \"техническая ошибка\", \"вытянула дважды\" и т.п.). Если что-то пошло неидеально, просто мягко продолжай расклад "
        "без технических комментариев.\n"
        "1. Никогда не объявляй \"расклад начат\" или \"позиция X означает Y\" как шаблон — говори живо, как в разговоре.\n"
        "2. Карты: есть два режима.\n"
        "   A) Пошаговый: используй draw_card(position_name) по одной карте за вызов.\n"
        "   B) Пакетный (без доп. уточнений): верни JSON-декоратор draw_cards, чтобы бот сам вытянул N карт сразу:\n"
        '      {"action":"draw_cards","count":5,"positions":["...","..."],"spread_name":"...","mode":"batch"}\n'
        "      count — целое 1..15. positions — опционально, но если передаёшь, их число должно быть ровно count.\n"
        "      В режиме draw_cards НЕ вызывай function tool draw_card в том же ответе.\n"
        "Если пользователь просит расклад на конкретное число карт (например «на 9 карт») или уже выбрана сетка из N позиций — "
        "предпочитай пакетный draw_cards, чтобы бот вытянул все N сразу без переспрашиваний; "
        "затем кратко пройдись по позициям; "
        "не останавливайся на одной карте и не уходи в длинную болтовню до того, как открыты все запрошенные позиции, "
        "если человек явно хочет полный расклад. Если он просит «медленно, по одной с обсуждением» — тогда можно по шагам.\n"
        "3. Вопросы пользователю: не более одного уточняющего вопроса за сообщение. "
        "Если человек просит сразу перейти к картам («давай расклад», «на 9 карт», «без вопросов», «не хочу уточнять») — "
        "не переспрашивай и не навязывай уточнения; сразу строй позиции и тяни карты по теме, которая уже есть в диалоге.\n"
        "3.1. Приоритет пакетного режима: если в реплике есть маркеры «сразу», «без вопросов», «пакетом», "
        "«одним сообщением», «сразу N карт», «не переспрашивай», то выбирай action=draw_cards (mode=batch) "
        "и не используй последовательный draw_card, кроме случаев, когда сам пользователь явно просит формат "
        "«по одной карте» или «с паузами между картами».\n"
        "4. JSON propose_spreads — только если реально есть выбор из двух или трёх разных раскладов. "
        "Никогда не используй propose_spreads с одним вариантом: при одном раскладе опиши позиции в живом тексте "
        "и сразу верни action=draw_cards с полным списком positions (пакетно). "
        "Во вступлении к propose_spreads обязательно назови каждый вариант и перечисли позиции — "
        "не пиши «такой расклад» без названия и позиций. "
        "В каждом элементе spreads поле positions — обязательно словарь {\"1\": \"название\", ...} "
        "с непустыми значениями.\n"
        "   Формат при 2–3 вариантах:\n"
        '   {"action": "propose_spreads", "spreads": [{"name": "...", "positions": {"1": "..."}, "why": "..."}]}\n'
        "5. Не путай названия классических раскладов с числом карт: классический Кельтский крест — 10 позиций, не 9. "
        "Для девяти карт используй корректное имя (например сетка 3×3 / девять позиций) и ровно 9 позиций в positions.\n"
        "6. После того как пользователь выбрал расклад — переходи к диалогу с картами (фаза dialogue_with_cards).\n"
        "7. Когда диалог завершён и ты готов подвести итог — верни JSON (в отдельном блоке, после живого текста прощания):\n"
        '   {"action": "complete", "memories": [{"type": "theme|pattern|preference|open_question|key_card", "content": "..."}]}\n'
        "Memories пиши живым языком от своего лица, как личные заметки — не сухие факты.\n"
        "Для open_question формулируй как вопрос, который ты сама хотела бы задать при следующей встрече.\n"
        "8. Не смешивай action-декораторы в одном JSON: один ответ — один action.\n"
        "9. Не вызывай draw_card в том же ходе, где отправляешь propose_spreads: сначала выбор расклада пользователем, потом карты.\n"
        "10. В draw_cards при mode=batch не задавай дополнительных вопросов пользователю в этом же сообщении.\n"
        "11. После ответа без расклада (фаза сбора контекста) часто предлагай 2–3 коротких вопроса на выбор — "
        "отдельным JSON (после текста):\n"
        '   {"action": "suggest_questions", "questions": ["...", "...", "..."]}\n'
        "Вопросы — про тему пользователя, не общие. Не дублируй их длинным списком в тексте, если отдал JSON.\n\n"
        "КАК ИСПОЛЬЗОВАТЬ ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n"
        "— Память относится к спрашивающему в чате, не подменяй ею субъект расклада (другие люди из вопроса).\n"
        "— Не представляйся заново и не перечисляй что ты о нём знаешь.\n"
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


def history_to_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Преобразовать историю БД в формат OpenAI Chat Completions.

    Старые записи прежнего провайдера не содержат идентификаторы tool calls. Для них создаётся
    стабильный ID, чтобы история продолжала корректно передаваться после релиза.
    """
    messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, str]] = []
    legacy_index = 0

    for item in history:
        role = item["role"]
        if role == "user":
            messages.append({"role": "user", "content": item.get("text") or ""})
            continue

        if role == "model":
            tool_calls: list[dict[str, Any]] = []
            for fc in item.get("function_calls") or []:
                name = str(fc.get("name") or "draw_card")
                args = fc.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                legacy_index += 1
                call_id = str(fc.get("id") or f"legacy_call_{legacy_index}")
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    }
                )
                pending_tool_calls.append({"id": call_id, "name": name})

            message: dict[str, Any] = {
                "role": "assistant",
                "content": (item.get("text") or "").strip() or None,
            }
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue

        if role == "tool":
            name = str(item.get("name") or "draw_card")
            match_index = next(
                (i for i, call in enumerate(pending_tool_calls) if call["name"] == name),
                None,
            )
            if match_index is None:
                logger.warning("Tool response without matching call in history: %s", name)
                continue
            call = pending_tool_calls.pop(match_index)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(item.get("response") or {}, ensure_ascii=False),
                }
            )
            continue

        raise ValueError(f"Unknown history role: {role}")

    return messages


def _response_text_and_calls(response: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    choices = response.get("choices") or []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    if not isinstance(message, dict):
        return "", []

    text = (message.get("content") or "").strip()
    calls: list[dict[str, Any]] = []
    for raw_call in message.get("tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function") or {}
        if not isinstance(function, dict):
            continue
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            logger.warning("OpenRouter returned invalid tool arguments: %s", raw_args)
            args = {}
        calls.append(
            {
                "id": str(raw_call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "args": args if isinstance(args, dict) else {},
            }
        )
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
    """Выделить action propose_spreads | draw_cards | complete из последнего подходящего JSON."""
    objs = extract_json_objects(text)
    for obj in reversed(objs):
        action = obj.get("action")
        if action in ("propose_spreads", "draw_cards", "complete", "suggest_questions"):
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
                if isinstance(o, dict) and o.get("action") in (
                    "propose_spreads",
                    "draw_cards",
                    "complete",
                    "suggest_questions",
                ):
                    return ""
            except json.JSONDecodeError:
                pass
            return m.group(0)

        return re.sub(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", repl, s, flags=re.IGNORECASE)

    s = _strip_fence(text)
    # Удалить «голый» JSON с action в конце сообщения
    for obj in extract_json_objects(s):
        if obj.get("action") in ("propose_spreads", "draw_cards", "complete", "suggest_questions"):
            blob = json.dumps(obj, ensure_ascii=False)
            if blob in s:
                s = s.replace(blob, "").strip()
    # Удалить хвостовой JSON action даже если форматирование/пробелы отличаются.
    s = re.sub(
        r"\s*\{\s*\"action\"\s*:\s*\"(?:propose_spreads|draw_cards|complete|suggest_questions)\"[\s\S]*\}\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    # Удалить однострочные action-json где угодно в тексте.
    s = re.sub(
        r"\{\s*\"action\"\s*:\s*\"(?:propose_spreads|draw_cards|complete|suggest_questions)\"[^\n]*\}",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    return s.strip()


def infer_phase_update(metadata: dict[str, Any] | None, current_phase: str) -> str | None:
    if not metadata:
        return None
    action = metadata.get("action")
    if action == "propose_spreads":
        return "proposing_spread"
    if action == "draw_cards":
        return "dialogue_with_cards"
    if action == "complete":
        return "summary"
    return None


async def call_openrouter(
    messages: list[dict[str, Any]],
    system_prompt: str,
) -> dict[str, Any]:
    """
    Один вызов OpenRouter по истории.

    Возвращает:
      text — текст модели (может быть пустым при только tool call),
      tool_calls — [{"name", "args"}, ...],
      metadata — распарсенный JSON с action propose_spreads | complete или None,
      raw_response — ответ API (для отладки и обратной совместимости).
    """
    history = history_to_messages(messages)
    try:
        response = await chat_completion(
            history,
            system_prompt=system_prompt,
            tools=[DRAW_CARD_TOOL],
        )
    except OpenRouterClientError:
        raise
    except Exception as exc:
        raise OpenRouterClientError(f"Ошибка обращения к OpenRouter: {exc}") from exc

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
        model_function_calls = [
            {"id": c.get("id") or "", "name": c["name"], "args": c.get("args") or {}}
            for c in tool_calls
        ]
    return {"content": content, "model_function_calls": model_function_calls}
