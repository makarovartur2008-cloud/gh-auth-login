import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("pick_models", Path(__file__).parent.parent / "scripts" / "pick_models.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)


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
