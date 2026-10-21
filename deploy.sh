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
  read -rp "Ключ ИИ (ProxyAPI): " AI_API_KEY
  read -rp "Телефон для листовок [89083003917]: " CONTACT_PHONE
  CONTACT_PHONE="${CONTACT_PHONE:-89083003917}"
  IP=$(curl -fsS https://api.ipify.org)
  DOMAIN="${IP//./-}.sslip.io"
  sed -e "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" \
      -e "s|^AI_API_KEY=.*|AI_API_KEY=${AI_API_KEY}|" \
      -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
      -e "s|^CONTACT_PHONE=.*|CONTACT_PHONE=${CONTACT_PHONE}|" \
      -e "s|^WEBAPP_URL=.*|WEBAPP_URL=https://${DOMAIN}|" \
      .env.example > .env
  echo "Адрес приложения: https://${DOMAIN}"
fi

mkdir -p data
docker compose up -d --build
echo
echo "Готово! Открой бота в Telegram и нажми Start — первый, кто нажмёт, станет владельцем."
