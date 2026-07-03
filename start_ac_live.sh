#!/usr/bin/env bash
# start_ac_live.sh — ОДНА команда для боевого АЦ.
#
# Запускает ОБА процесса на захвате экрана и гасит оба по Ctrl+C:
#   • combo_live_daemon.py   (venv_new) — поза + пульс-по-видео + речь, один захват @10fps
#   • combo_live_emotion.py  (emo_venv) — эмоции (HSEmotion), companion через emo_state.json
# Эмоции попадают в общую историю демона (CSV) автоматически.
#
# История сессии (автосейв построчно, реальное время) → combo/logs/combo_session_*.csv
# Живая панель → webapp/combo_live.html
#
# Использование:
#   ./start_ac_live.sh [-e упражнение] [--zone x,y,w,h] [--no-emotion] [--no-audio]
# Без --zone оба авто-находят участника (Granatum закрепляет его сверху).
set -u
cd "$(dirname "$0")"

MAIN_PY="${COMBO_MAIN_PYTHON:-venv_new/bin/python}"
EMO_PY="${COMBO_EMO_PYTHON:-emo_venv/bin/python}"

EX=""; ZONE=""; WITH_EMO=1; DAEMON_AUDIO=""
while [ $# -gt 0 ]; do
  case "$1" in
    -e|--exercise) EX="$2"; shift 2;;
    --zone)        ZONE="$2"; shift 2;;
    --no-emotion)  WITH_EMO=0; shift;;
    --no-audio)    DAEMON_AUDIO="--no-audio"; shift;;
    *) echo "неизвестный флаг: $1"; shift;;
  esac
done

# зону не задали явно → обвести плитку участника мышью (чтобы авто-выбор не прыгал Иван/Руслан/середина)
if [ -z "$ZONE" ]; then
  echo "→ Откроется экран: ОБВЕДИ МЫШЬЮ плитку участника, нажми ENTER (c — пропустить = авто-выбор)."
  ZONE=$("$MAIN_PY" zone_picker.py 2>/dev/null)
  if [ -n "$ZONE" ]; then echo "  зона участника зафиксирована: $ZONE"
  else echo "  зона не выбрана → авто-выбор (может прыгать между лицами)."; fi
fi
ZONE_ARG=""; [ -n "$ZONE" ] && ZONE_ARG="--zone $ZONE"
EX_ARG="";   [ -n "$EX" ]   && EX_ARG="-e $EX"

PIDS=()
cleanup() {
  echo; echo "останавливаю АЦ-инструмент…"
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
  echo "готово. История сессии — в combo/logs/combo_session_*.csv"
  exit 0
}
trap cleanup INT TERM

echo "=== Боевой АЦ live-инструмент ==="
echo "Зона: ${ZONE:-авто (участник сверху)} | Упражнение: ${EX:-—} | Эмоции: $([ $WITH_EMO -eq 1 ] && echo да || echo нет) | Звук: $([ -z "$DAEMON_AUDIO" ] && echo да || echo нет)"

# живая панель по HTTP — file:// блокирует fetch combo_data.json, поэтому поднимаем сервер
WEBAPP_DIR="${COMBO_WEBAPP_DIR:-webapp}"
PANEL_PORT="${COMBO_PANEL_PORT:-8770}"
PANEL_URL=""
if [ -f "$WEBAPP_DIR/combo_live.html" ]; then
  ( cd "$WEBAPP_DIR" && exec python3 -m http.server "$PANEL_PORT" --bind 127.0.0.1 ) >/dev/null 2>&1 &
  PIDS+=($!)
  PANEL_URL="http://127.0.0.1:$PANEL_PORT/combo_live.html"
  ( sleep 1; command -v xdg-open >/dev/null && xdg-open "$PANEL_URL" ) >/dev/null 2>&1 &
fi

if [ $WITH_EMO -eq 1 ]; then
  if [ -x "$EMO_PY" ]; then
    "$EMO_PY" combo_live_emotion.py --source screen $ZONE_ARG &
    PIDS+=($!)
    sleep 1   # дать companion подняться, чтобы emo_state.json был к старту демона
  else
    echo "⚠️ нет $EMO_PY — эмоции пропущены (остальное работает)"
  fi
fi

"$MAIN_PY" combo_live_daemon.py --source screen $ZONE_ARG $EX_ARG $DAEMON_AUDIO &
PIDS+=($!)

echo "Живая панель: ${PANEL_URL:-открой webapp/combo_live.html ТОЛЬКО по HTTP, не file://}"
echo "Ctrl+C — остановить всё (демон, эмоции, панель)."
wait
