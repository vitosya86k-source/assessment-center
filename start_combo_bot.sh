#!/usr/bin/env bash
# Запуск КОМБО-бота @assessment_center_analyzer_bot. Запускать: bash start_combo_bot.sh
# Интерпретер из COMBO_MAIN_PYTHON (если venv вне Dropbox), иначе локальный venv_new.
cd "$(dirname "$0")" || exit 1
PY="${COMBO_MAIN_PYTHON:-venv_new/bin/python}"
exec "$PY" combo_bot.py
