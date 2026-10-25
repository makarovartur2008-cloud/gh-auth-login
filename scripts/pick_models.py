"""Запуск выбора моделей с сервера без зависимостей: python3 scripts/pick_models.py BASE_URL KEY (см. app/pick_models.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pick_models import main  # noqa: E402

if __name__ == "__main__":
    main()
