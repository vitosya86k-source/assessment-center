# 🚂 Деплой на Railway - Решение для удаленного сервера

## ❌ Проблема: Bluetooth не работает на Railway

**Railway** - это облачный сервер, у него **нет физического доступа к Bluetooth**. 

Bluetooth работает только на **коротких расстояниях** (10-30 метров), поэтому:
- ❌ Прямая интеграция через Bluetooth **НЕ будет работать**
- ❌ Сервер на Railway не может подключиться к Polar устройству

## ✅ Решение: Android приложение + API

### Архитектура:

```
┌─────────────┐      Bluetooth      ┌──────────────┐
│ Polar H10   │ ◄─────────────────► │   Телефон    │
│  (браслет)  │                      │ (Android app)│
└─────────────┘                      └──────┬───────┘
                                            │
                                            │ HTTP/API
                                            ▼
                                    ┌──────────────┐
                                    │   Railway    │
                                    │  (API + бот) │
                                    └──────┬───────┘
                                           │
                                           │ Telegram API
                                           ▼
                                    ┌──────────────┐
                                    │  Telegram   │
                                    │   (бот)     │
                                    └─────────────┘
```

**Пользователь** → Telegram бот (на Railway) ← API ← Android приложение ← Polar (Bluetooth)

## 🎯 Как это работает

1. **Телефон с Android приложением** подключается к Polar через Bluetooth
2. **Android приложение** собирает данные HRV
3. **Android приложение** отправляет данные на API сервер (Railway)
4. **Бот на Railway** получает данные через API
5. **Пользователь** получает дашборды в Telegram

## 📱 Настройка Android приложения

Android приложение должно отправлять данные на ваш Railway API:

```python
# Пример кода для Android приложения
import requests

API_URL = "https://your-app.railway.app/api/v1/hrv-data-simple"

# После сбора данных с Polar
data = {
    "user_id": 123456789,  # Telegram user ID
    "heart_rates": [75, 76, 77, ...],  # Список HR
    "timestamps": ["2025-01-22 12:00:00", ...],  # Временные метки
    "device_name": "Polar H10"
}

response = requests.post(API_URL, json=data)
```

## 🚀 Деплой на Railway

### 1. Создайте файл `railway.json`:

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "python api_server.py & python telegram_bot.py",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

### 2. Создайте `Procfile`:

```
web: python api_server.py
bot: python telegram_bot.py
```

### 3. Переменные окружения в Railway:

```
TELEGRAM_BOT_TOKEN=ваш_токен
PORT=8000
```

### 4. Деплой:

```bash
# Установите Railway CLI
npm i -g @railway/cli

# Логин
railway login

# Инициализация
railway init

# Деплой
railway up
```

## 📡 API Endpoints

### POST `/api/v1/hrv-data`

Полный формат с объектами:

```json
{
  "user_id": 123456789,
  "device_name": "Polar H10",
  "data": [
    {
      "heart_rate_bpm": 75.0,
      "timestamp_iso": "2025-01-22 12:00:00",
      "rr_interval_ms": 800.0
    },
    ...
  ]
}
```

### POST `/api/v1/hrv-data-simple`

Упрощенный формат:

```json
{
  "user_id": 123456789,
  "heart_rates": [75, 76, 77, ...],
  "timestamps": ["2025-01-22 12:00:00", ...],
  "device_name": "Polar H10"
}
```

## 🔧 Интеграция с ботом

Бот автоматически:
1. Получает данные через API
2. Сохраняет в `data/user_{user_id}_latest.csv`
3. Отправляет уведомление пользователю
4. Пользователь использует `/analyze` для получения дашбордов

## 📝 Пример использования

### Android приложение:

```kotlin
// После сбора данных с Polar
val heartRates = listOf(75.0, 76.0, 77.0, ...)
val timestamps = listOf("2025-01-22 12:00:00", ...)

val request = mapOf(
    "user_id" to telegramUserId,
    "heart_rates" to heartRates,
    "timestamps" to timestamps,
    "device_name" to "Polar H10"
)

val response = httpClient.post("https://your-app.railway.app/api/v1/hrv-data-simple") {
    contentType(ContentType.Application.Json)
    body = request
}
```

### Telegram бот:

```
Пользователь: /analyze
Бот: [Отправляет 4 PNG дашборда]
```

## ⚙️ Настройка Railway

1. **Создайте проект** на Railway
2. **Подключите GitHub репозиторий** или загрузите код
3. **Установите переменные окружения**:
   - `TELEGRAM_BOT_TOKEN`
   - `PORT=8000`
4. **Railway автоматически** определит Python и установит зависимости
5. **Запустится** API сервер и бот

## 🔒 Безопасность

Для продакшена добавьте:

1. **API ключ** для защиты endpoints
2. **Валидацию user_id** (проверка что это реальный Telegram ID)
3. **Rate limiting** для предотвращения спама
4. **HTTPS** (Railway предоставляет автоматически)

## 📊 Мониторинг

Railway предоставляет:
- Логи в реальном времени
- Метрики использования
- Автоматические рестарты при ошибках

## ✅ Преимущества этого подхода

✅ **Работает на любом расстоянии** - телефон и сервер могут быть далеко  
✅ **Мобильность** - можно собирать данные где угодно  
✅ **Надежность** - Railway обеспечивает uptime  
✅ **Масштабируемость** - может обслуживать много пользователей  

## 🆚 Сравнение

| Вариант | Railway | Расстояние | Сложность |
|---------|---------|------------|-----------|
| **Прямая интеграция** | ❌ Не работает | Ограничено | - |
| **Android + API** | ✅ Работает | Не ограничено | Средняя |
| **Android + файлы** | ✅ Работает | Не ограничено | Простая |

---

**Итого: Для Railway используйте Android приложение + API! 🚀**
