import os
import tempfile

# Отдельная база для тестов — до импорта приложения
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["BOT_TOKEN"] = ""
os.environ["DEV_USER_TG_ID"] = "0"

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    init_db(engine)
    yield


@pytest.fixture
def s():
    with SessionLocal() as session:
        yield session
