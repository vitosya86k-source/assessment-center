#!/usr/bin/env bash
# Просмотр веб-интерфейсов. Запускать: bash start_webapp.sh
#   HRV-дашборд:  http://localhost:8777/hrv.html
#   Live-экран:   http://localhost:8777/combo_live.html
# Обслуживает COMBO_WEBAPP_DIR (туда боты/демон пишут *_data.json). По умолчанию ./webapp.
cd "$(dirname "$0")" || exit 1
DIR="${COMBO_WEBAPP_DIR:-$(pwd)/webapp}"
echo "webapp ($DIR) на http://localhost:8777  (hrv.html / combo_live.html)"
cd "$DIR" || exit 1
exec python3 -m http.server 8777
