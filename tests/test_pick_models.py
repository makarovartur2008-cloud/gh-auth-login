from app import pick_models as pm


def test_picks_cheap_chat_and_stt():
    ids = ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4o-mini-transcribe", "text-embedding-3-small",
           "openai/whisper-large-v3", "openai/whisper-large-v3-turbo", "gpt-image-1", "tts-1"]
    assert pm.pick_chat(ids) == "gpt-5.4-nano"
    assert pm.pick_stt(ids) == "gpt-4o-mini-transcribe"


def test_old_names_still_work():
    assert pm.pick_chat(["gpt-4o", "gpt-4o-mini", "whisper-1"]) == "gpt-4o-mini"
    assert pm.pick_stt(["gpt-4o", "gpt-4o-mini", "whisper-1"]) == "whisper-1"


def test_whisper_large_when_no_transcribe():
    assert pm.pick_stt(["openai/whisper-large-v3", "openai/whisper-large-v3-turbo"]) == "openai/whisper-large-v3-turbo"
    assert pm.pick_chat(["openai/whisper-large-v3"]) == ""


def test_choose_models_only_fills_unset(monkeypatch):
    import asyncio

    from app import ai
    from app.config import Settings

    s = Settings(_env_file=None, ai_api_key="k", ai_stt_model="my-whisper")
    monkeypatch.setattr(ai, "settings", s)
    monkeypatch.setattr(pm, "fetch_ids", lambda base, key: ["gpt-5.4-mini", "gpt-5.4-nano", "openai/whisper-large-v3"])
    asyncio.run(ai.choose_models())
    assert s.ai_model == "gpt-5.4-nano"
    assert s.ai_stt_model == "my-whisper"  # задано явно — не трогаем


def test_choose_models_keeps_defaults_when_offline(monkeypatch):
    import asyncio

    from app import ai
    from app.config import Settings

    def boom(base, key):
        raise OSError("нет сети")

    s = Settings(_env_file=None, ai_api_key="k")
    monkeypatch.setattr(ai, "settings", s)
    monkeypatch.setattr(pm, "fetch_ids", boom)
    asyncio.run(ai.choose_models())
    assert (s.ai_model, s.ai_stt_model) == ("gpt-4o-mini", "whisper-1")
