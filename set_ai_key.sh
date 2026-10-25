#!/usr/bin/env bash
# Добавить или поменять ключ ИИ (распознавание голоса и разбор сообщений): bash set_ai_key.sh
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "Сначала запусти bash deploy.sh"; exit 1; }
read -rp "Ключ ИИ (ProxyAPI или другой OpenAI-совместимый): " AI_API_KEY
read -rp "Адрес API [$(grep -E '^AI_BASE_URL=' .env | cut -d= -f2-)]: " BASE
BASE="${BASE:-$(grep -E '^AI_BASE_URL=' .env | cut -d= -f2-)}"
sed -i -e "s|^AI_API_KEY=.*|AI_API_KEY=${AI_API_KEY}|" -e "s|^AI_BASE_URL=.*|AI_BASE_URL=${BASE}|" .env
python3 scripts/pick_models.py "$BASE" "$AI_API_KEY" | while IFS='=' read -r k v; do
  sed -i "s|^${k}=.*|${k}=${v}|" .env
done
grep -E '^AI_(BASE_URL|MODEL|STT_MODEL)=' .env
docker compose up -d
echo "Готово: ключ сохранён, бот перезапущен."
