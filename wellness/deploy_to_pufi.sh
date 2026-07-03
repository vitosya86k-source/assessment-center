#!/usr/bin/env bash
# Довозит откалиброванный wellness-код на сервер Пуфи (Hetzner).
# Canonical-источник — ЗДЕСЬ (NEW VERSION/wellness/); сервер — производное зеркало.
#
# Что и зачем:
#   - bp_fit.json     — КАЛИБРОВКА давления (104/64). estimate() перечитывает её с диска
#                       при каждом кружке → подхватится СЛЕДУЮЩИМ замером без рестарта бота.
#   - bp_estimate.py  — фикс calibrate() (робастный сдвиг базы). Нужен для консистентности;
#                       на рантайм кружка не влияет (estimate() не менялся).
#
# Безопасно: сначала показывает состояние сервера и --dry-run, заливает только по подтверждению.
# Запуск:  bash "NEW VERSION/wellness/deploy_to_pufi.sh"
set -euo pipefail

SSH_KEY="${PUFI_SSH_KEY:-$HOME/.ssh/hetzner-openclaw}"
SSH_HOST="${PUFI_SSH_HOST:-root@157.180.64.229}"
REMOTE_DIR="${PUFI_WELLNESS_DIR:-/home/openclaw/openclaw-vault/pufy-signal-core/wellness}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RSYNC_SSH="ssh -i $SSH_KEY -o ConnectTimeout=15"

echo "=== Deploy wellness → Пуфи ==="
echo "Источник: $LOCAL_DIR"
echo "Цель:     $SSH_HOST:$REMOTE_DIR"
echo ""

# Локально bp_fit.json обязан существовать (иначе калибровка не применена)
if [[ ! -f "$LOCAL_DIR/bp_fit.json" ]]; then
  echo "❌ Нет $LOCAL_DIR/bp_fit.json — сначала прогони калибровку локально." >&2
  exit 1
fi

echo "==> Состояние на сервере СЕЙЧАС:"
ssh -i "$SSH_KEY" -o ConnectTimeout=15 "$SSH_HOST" "
  cd '$REMOTE_DIR' 2>/dev/null || { echo '  ⚠ каталог $REMOTE_DIR не найден'; exit 0; }
  echo -n '  bp_estimate.py: '; md5sum bp_estimate.py 2>/dev/null | cut -d' ' -f1 || echo 'нет'
  echo -n '  bp_fit.json:    '; (md5sum bp_fit.json 2>/dev/null | cut -d' ' -f1) || echo 'НЕТ (калибровка ещё не доехала)'
"
echo ""

echo "==> Что изменится (dry-run):"
rsync -avz --dry-run -e "$RSYNC_SSH" \
  "$LOCAL_DIR/bp_estimate.py" "$LOCAL_DIR/bp_fit.json" \
  "$SSH_HOST:$REMOTE_DIR/"
echo ""

read -r -p "Заливать эти 2 файла на сервер? [y/N] " ans
if [[ "${ans,,}" != "y" ]]; then
  echo "Отменено. Ничего не залито."
  exit 0
fi

echo "==> Заливаю…"
rsync -avz -e "$RSYNC_SSH" \
  "$LOCAL_DIR/bp_estimate.py" "$LOCAL_DIR/bp_fit.json" \
  "$SSH_HOST:$REMOTE_DIR/"

echo ""
echo "==> Проверка после заливки (md5 должны совпасть с локальными):"
echo -n "  локально  bp_fit.json:    "; md5sum "$LOCAL_DIR/bp_fit.json" | cut -d' ' -f1
echo -n "  локально  bp_estimate.py: "; md5sum "$LOCAL_DIR/bp_estimate.py" | cut -d' ' -f1
ssh -i "$SSH_KEY" -o ConnectTimeout=15 "$SSH_HOST" "
  cd '$REMOTE_DIR'
  echo -n '  на сервере bp_fit.json:    '; md5sum bp_fit.json | cut -d' ' -f1
  echo -n '  на сервере bp_estimate.py: '; md5sum bp_estimate.py | cut -d' ' -f1
"
echo ""
echo "✅ Готово. Калибровку подхватит СЛЕДУЮЩИЙ кружок (estimate читает bp_fit.json с диска)."
echo "   Рестарт бота НЕ требуется для калибровки."
