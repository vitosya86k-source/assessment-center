# Assessment Center — Railway (combo bot + webhook + мини-апп)

Отдельный сервис от **polar-hrv** (`HRV_backend`). Один процесс:

| Путь | Назначение |
|------|------------|
| `POST /telegram/webhook` | `@assessment_center_analyzer_bot` (webhook, не polling) |
| `GET /phone_analyze.html` | мини-апп live-анализа (`/analyze` в боте) |
| `GET /combo_live.html` | живая панель |
| `POST /ac/analyze` | API разбора клипа (телефон → сервер) |
| `GET /health` | пробуждение из sleep |

## Git → Railway

Репозиторий: **https://github.com/vitosya86k-source/assessment-center**

С ноутбука:

```bash
./combo-railway-sync-to-railway.sh "AC: webhook + miniapp"
```

Railway: New Project → Deploy from GitHub → `vitosya86k-source/assessment-center`.

## Переменные окружения (Railway)

| Переменная | Обязательно | Пример |
|------------|-------------|--------|
| `COMBO_BOT_TOKEN` | да | токен `@assessment_center_analyzer_bot` |
| `COMBO_MINIAPP_URL` | после 1-го деплоя | `https://<сервис>.up.railway.app` |
| `COMBO_RUNTIME_DIR` | рекомендуется | `/tmp/combo` |
| `COMBO_EMO_PYTHON` | на Railway | `python3` |
| `COMBO_MAIN_PYTHON` | на Railway | `python3` |
| `WEBHOOK_SECRET` | опц. | случайная строка |

`COMBO_MINIAPP_URL` можно не задавать сразу: если есть `RAILWAY_PUBLIC_DOMAIN`, подставится автоматически при старте.

**Sleep:** Settings → Serverless / App Sleeping → **On** (как у пульс-бота).

## После первого деплоя

1. Скопировать публичный URL Railway → `COMBO_MINIAPP_URL` (если авто не сработало).
2. Redeploy.
3. **Остановить локальный polling** (`start_combo_bot.sh`) — иначе Telegram 409 Conflict.
4. В боте: `/analyze` → кнопка Web App с `phone_analyze.html`.

## Проверка

```bash
curl https://<domain>/health
curl -I https://<domain>/phone_analyze.html
```

Видео в Telegram → бот на Railway принимает через webhook.

## Синхронизация с ноутбука

Скрипт копирует `NEW VERSION/` (код + webapp + wellness + модели) и поверх — файлы из `combo-railway/` (`combo_server.py`, `railway.json`, …).
