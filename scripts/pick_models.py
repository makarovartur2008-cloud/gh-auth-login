"""Спрашивает у ИИ-сервиса (OpenAI-совместимого) список моделей и выбирает дешёвые: для текста и для голоса.

Использование: python3 scripts/pick_models.py BASE_URL KEY  →  печатает строки AI_MODEL=... и AI_STT_MODEL=...
Если сервис не ответил — ничего не печатает (остаются значения из .env).
Только стандартная библиотека: запускается на чистом сервере.
"""

import json
import sys
import urllib.request

SKIP_CHAT = ("transcribe", "whisper", "audio", "tts", "speech", "embedding", "image", "dall", "realtime",
             "search", "moderation", "asr", "vision-preview", "sora", "codex")
CHAT_PREFER = ("nano", "mini", "luna", "flash", "lite")
STT_PREFER = ("whisper-1", "gpt-4o-mini-transcribe", "transcribe", "whisper-large-v3-turbo", "whisper", "asr")


def pick_chat(ids: list[str]) -> str:
    chat = [m for m in ids if not any(s in m.lower() for s in SKIP_CHAT)]
    for word in CHAT_PREFER:
        found = sorted(m for m in chat if word in m.lower())
        if found:
            return found[0]
    return ""


def pick_stt(ids: list[str]) -> str:
    for word in STT_PREFER:
        found = sorted(m for m in ids if word in m.lower())
        if found:
            return found[0]
    return ""


def fetch_ids(base: str, key: str) -> list[str]:
    req = urllib.request.Request(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


def main():
    if len(sys.argv) < 3 or not sys.argv[2]:
        return
    try:
        ids = fetch_ids(sys.argv[1], sys.argv[2])
    except Exception as e:  # нет сети, неверный ключ — оставляем как есть
        print(f"# не удалось получить список моделей: {e}", file=sys.stderr)
        return
    chat, stt = pick_chat(ids), pick_stt(ids)
    if chat:
        print(f"AI_MODEL={chat}")
    if stt:
        print(f"AI_STT_MODEL={stt}")
    print(f"# моделей доступно: {len(ids)}; выбрано: текст={chat or '—'}, голос={stt or '—'}", file=sys.stderr)


if __name__ == "__main__":
    main()
