# 📱 Интеграция Android приложения с Railway API

## 🎯 Для работы на Railway нужен Android

**Да, для Railway нужен Android приложение**, потому что:
- Railway - облачный сервер без Bluetooth
- Телефон с Android приложением подключается к Polar через Bluetooth
- Android приложение отправляет данные на Railway API

## 📋 Что нужно сделать в Android приложении

### 1. Сбор данных с Polar

Используйте Polar SDK для Android:
- Подключение через Bluetooth
- Сбор данных HR каждую секунду
- Сохранение в массив

### 2. Отправка данных на API

После сбора данных (например, 5 минут):

```kotlin
// Пример на Kotlin
data class HRVData(
    val user_id: Long,
    val heart_rates: List<Double>,
    val timestamps: List<String>,
    val device_name: String = "Polar H10"
)

// После сбора данных
val hrvData = HRVData(
    user_id = telegramUserId,  // Получить из настроек приложения
    heart_rates = collectedHeartRates,
    timestamps = collectedTimestamps,
    device_name = "Polar H10"
)

// Отправка на Railway
val client = OkHttpClient()
val json = Gson().toJson(hrvData)
val requestBody = json.toRequestBody("application/json".toMediaType())

val request = Request.Builder()
    .url("https://your-app.railway.app/api/v1/hrv-data-simple")
    .post(requestBody)
    .build()

val response = client.newCall(request).execute()
```

### 3. Упрощенный вариант (только HR)

Если у вас только HR данные:

```kotlin
val requestBody = """
{
    "user_id": $telegramUserId,
    "heart_rates": [${heartRates.joinToString(",")}],
    "timestamps": [${timestamps.joinToString(",")}]
}
""".trimIndent()

val request = Request.Builder()
    .url("https://your-app.railway.app/api/v1/hrv-data-simple")
    .post(requestBody.toRequestBody("application/json".toMediaType()))
    .build()
```

## 🔑 Получение Telegram User ID

Пользователь должен:
1. Написать боту `/start`
2. Бот отправит его User ID
3. Пользователь вводит ID в Android приложение

Или бот может отправить специальную ссылку с токеном для авторизации.

## 📡 API Endpoints

### POST `/api/v1/hrv-data-simple`

**Формат запроса:**
```json
{
    "user_id": 123456789,
    "heart_rates": [75.0, 76.0, 77.0, ...],
    "timestamps": ["2025-01-22 12:00:00", "2025-01-22 12:00:01", ...],
    "device_name": "Polar H10"
}
```

**Ответ:**
```json
{
    "status": "success",
    "data_points": 300
}
```

## ✅ Что происходит после отправки

1. API получает данные
2. Сохраняет в `data/user_{user_id}_latest.csv`
3. Отправляет уведомление пользователю в Telegram
4. Пользователь использует `/analyze` в боте
5. Получает 4 PNG дашборда

## 🔄 Полный цикл

```
1. Пользователь открывает Android приложение
2. Приложение подключается к Polar (Bluetooth)
3. Собирает данные 5 минут
4. Отправляет на Railway API
5. Пользователь открывает Telegram
6. Получает уведомление от бота
7. Использует /analyze
8. Получает дашборды
```

## 🛠️ Пример минимального Android приложения

```kotlin
class MainActivity : AppCompatActivity() {
    private val apiUrl = "https://your-app.railway.app/api/v1/hrv-data-simple"
    private val telegramUserId = 123456789L  // Получить из настроек
    
    fun sendHRVData(heartRates: List<Double>, timestamps: List<String>) {
        val requestBody = JSONObject().apply {
            put("user_id", telegramUserId)
            put("heart_rates", JSONArray(heartRates))
            put("timestamps", JSONArray(timestamps))
            put("device_name", "Polar H10")
        }
        
        val request = Request.Builder()
            .url(apiUrl)
            .post(requestBody.toString().toRequestBody("application/json".toMediaType()))
            .build()
        
        OkHttpClient().newCall(request).enqueue(object : Callback {
            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    // Успешно отправлено
                    runOnUiThread {
                        Toast.makeText(this@MainActivity, 
                            "Данные отправлены! Проверьте Telegram", 
                            Toast.LENGTH_LONG).show()
                    }
                }
            }
            
            override fun onFailure(call: Call, e: IOException) {
                // Ошибка
            }
        })
    }
}
```

## 📝 Чеклист для Android разработчика

- [ ] Подключение к Polar через Bluetooth
- [ ] Сбор данных HR каждую секунду
- [ ] Сохранение временных меток
- [ ] Отправка данных на Railway API после сбора
- [ ] Обработка ошибок сети
- [ ] Получение Telegram User ID от пользователя
- [ ] Уведомление пользователя об успешной отправке

---

**Готово! Android приложение отправляет данные на Railway, бот обрабатывает! 🚀**
