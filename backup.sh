#!/usr/bin/env bash
# Копия базы: bash backup.sh  (файл появится в папке backups)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
cp data/app.db "backups/app-$(date +%F-%H%M).db"
ls -1t backups | tail -n +31 | sed 's|^|backups/|' | xargs -r rm --
echo "Сохранено в backups/"
