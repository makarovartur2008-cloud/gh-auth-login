#!/usr/bin/env bash
# Установка на чистый сервер Ubuntu: bash deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  echo "Ставлю Docker..."
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  read -rp "Токен бота от @BotFather: " BOT_TOKEN
  read -rp "Ключ ИИ (ProxyAPI; Enter — добавить позже командой bash set_ai_key.sh): " AI_API_KEY
  read -rp "Телефон для листовок [89083003917]: " CONTACT_PHONE
  CONTACT_PHONE="${CONTACT_PHONE:-89083003917}"
  read -rp "Город для поиска адресов [Самара]: " CITY
  CITY="${CITY:-Самара}"
  read -rp "Откуда выезжаешь [Олимпийская улица, 68]: " DEPOT_ADDRESS
  DEPOT_ADDRESS="${DEPOT_ADDRESS:-Олимпийская улица, 68}"
  IP=$(curl -fsS https://api.ipify.org)
  DOMAIN="${IP//./-}.sslip.io"
  sed -e "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" \
      -e "s|^AI_API_KEY=.*|AI_API_KEY=${AI_API_KEY}|" \
      -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
      -e "s|^CONTACT_PHONE=.*|CONTACT_PHONE=${CONTACT_PHONE}|" \
      -e "s|^CITY=.*|CITY=${CITY}|" \
      -e "s|^DEPOT_ADDRESS=.*|DEPOT_ADDRESS=${DEPOT_ADDRESS}|" \
      -e "s|^WEBAPP_URL=.*|WEBAPP_URL=https://${DOMAIN}|" \
      .env.example > .env
  echo "Адрес приложения: https://${DOMAIN}"
  # какие модели есть у ИИ-сервиса — выбираем самые дешёвые для текста и голоса
  if [ -n "${AI_API_KEY}" ]; then
    BASE=$(grep -E '^AI_BASE_URL=' .env | cut -d= -f2-)
    python3 scripts/pick_models.py "$BASE" "$AI_API_KEY" | while IFS='=' read -r k v; do
      sed -i "s|^${k}=.*|${k}=${v}|" .env
    done
  fi
fi

mkdir -p data
docker compose up -d --build
echo
echo "Готово! Открой бота в Telegram и нажми Start — первый, кто нажмёт, станет владельцем."
