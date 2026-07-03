#!/usr/bin/env bash
# Боевой live-режим комбо: захват экрана → combo_data.json (→ combo_live.html). Запускать: bash start_combo_live.sh
# Зона участника: --zone 0,0,960,540. Без --zone — весь экран (авто-выбор участника).
cd "$(dirname "$0")" || exit 1
PY="${COMBO_MAIN_PYTHON:-venv_new/bin/python}"
exec "$PY" combo_live_daemon.py --source screen "$@"
