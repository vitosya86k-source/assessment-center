#!/usr/bin/env bash
# Сборка окружений из requirements. Запуск: bash setup.sh [python3]
#
# По умолчанию venv'ы собираются рядом с кодом. НА СЕРВЕРЕ, где код лежит на
# read-only Dropbox, собирай в writable папку:
#   VENV_DIR=/home/node/openclaw-vault/venvs bash setup.sh python3.12
# Тогда укажи интерпретеры для пусковых скриптов:
#   export COMBO_MAIN_PYTHON=$VENV_DIR/venv_new/bin/python
#   export COMBO_EMO_PYTHON=$VENV_DIR/emo_venv/bin/python
#   export HRV_MAIN_PYTHON=$VENV_DIR/venv_new/bin/python
set -e
cd "$(dirname "$0")"
SRC="$(pwd)"
PY="${1:-python3}"
VENV_DIR="${VENV_DIR:-$SRC}"          # куда класть venv (вне Dropbox на сервере)
mkdir -p "$VENV_DIR"
MAIN="$VENV_DIR/venv_new"
EMO="$VENV_DIR/emo_venv"

echo ">>> venv_new в $MAIN"
[ -d "$MAIN" ] || "$PY" -m venv "$MAIN"
"$MAIN/bin/pip" install -q --upgrade pip
"$MAIN/bin/pip" install -q -r requirements-main.txt

echo ">>> emo_venv в $EMO"
[ -d "$EMO" ] || "$PY" -m venv "$EMO"
"$EMO/bin/pip" install -q --upgrade pip
"$EMO/bin/pip" install -q -r requirements-emo.txt

echo ">>> проверка импортов"
"$MAIN/bin/python" - <<'EOF'
import numpy, pandas, cv2, scipy, mss, telegram, docx, openpyxl
print("venv_new: numpy/pandas/cv2/scipy/mss/telegram/docx/openpyxl OK")
try:
    import mediapipe, sounddevice, neurokit2
    print("venv_new: mediapipe/sounddevice/neurokit2 OK")
except Exception as e:
    print("venv_new: опционально не встало:", e)
EOF
"$EMO/bin/python" -c "import hsemotion_onnx, onnxruntime, cv2; print('emo_venv: hsemotion/onnxruntime/cv2 OK')"

# Засеять webapp HTML в writable webapp-каталог (если он вне Dropbox)
if [ -n "$COMBO_WEBAPP_DIR" ] && [ "$COMBO_WEBAPP_DIR" != "$SRC/webapp" ]; then
  mkdir -p "$COMBO_WEBAPP_DIR"
  cp -f webapp/hrv.html webapp/combo_live.html webapp/*.sample.json "$COMBO_WEBAPP_DIR"/ 2>/dev/null || true
  echo ">>> webapp HTML скопирован в $COMBO_WEBAPP_DIR"
fi

echo ""
echo ">>> ГОТОВО. Дальше:"
echo "  1) cp .env.example .env  и впиши РОТИРОВАННЫЕ токены (@BotFather)"
echo "  2) на сервере (read-only Dropbox) задай writable пути и интерпретеры:"
echo "     export COMBO_RUNTIME_DIR=/home/node/openclaw-vault/combo"
echo "     export HRV_RUNTIME_DIR=/home/node/openclaw-vault/hrv"
echo "     export COMBO_WEBAPP_DIR=/home/node/openclaw-vault/webapp"
echo "     export COMBO_MAIN_PYTHON=$MAIN/bin/python HRV_MAIN_PYTHON=$MAIN/bin/python"
echo "     export COMBO_EMO_PYTHON=$EMO/bin/python"
echo "  3) запуск: bash start_bot.sh | bash start_combo_bot.sh | bash start_combo_live.sh | bash start_webapp.sh"
