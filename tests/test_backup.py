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
