import asyncio

from app import bot as staff_bot
from app import services as sv
from app.db import User


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_document(self, chat_id, document, caption="", **kw):
        with open(document.path, "rb") as f:
            self.sent.append((chat_id, f.read(16), caption))


def test_backup_goes_to_owner_only(s):
    s.add_all([User(tg_id=1, name="Артур", role="owner"), User(tg_id=2, name="Друг", role="member")])
    s.commit()
    sv.upsert_client(s, {"name": "Марина", "phone": "89001234567", "address": "Ленина 5"})
    s.commit()
    bot = FakeBot()
    asyncio.run(staff_bot.send_backup(bot))
    assert [chat for chat, _, _ in bot.sent] == [1]
    _, head, caption = bot.sent[0]
    assert head.startswith(b"SQLite format 3")
    assert "клиентов 1" in caption


def test_app_opens_via_inline_button(monkeypatch):
    """С кнопки нижней клавиатуры Telegram не передаёт initData — приложение открываем inline-кнопкой."""
    from app.config import settings

    monkeypatch.setattr(settings, "webapp_url", "https://example.org/")
    kb = staff_bot.main_kb()
    app_btn = kb.keyboard[-1][0]
    assert app_btn.text == staff_bot.BTN_APP and app_btn.web_app is None
    assert staff_bot.app_kb().inline_keyboard[0][0].web_app.url == "https://example.org/"
