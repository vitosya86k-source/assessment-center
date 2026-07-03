#!/usr/bin/env bash
# Запуск пульсового бота @HRV_monitor_bot.
# Запускать: bash start_bot.sh
# Интерпретер из HRV_MAIN_PYTHON (если venv вне Dropbox), иначе локальный venv_new.
cd "$(dirname "$0")" || exit 1
PY="${HRV_MAIN_PYTHON:-${COMBO_MAIN_PYTHON:-venv_new/bin/python}}"
exec "$PY" run_bot.py
