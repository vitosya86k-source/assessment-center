"""
Telegram бот для NeuroHRV Monitor
"""
import logging
import os
from pathlib import Path
from datetime import datetime
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import asyncio

from config import TELEGRAM_BOT_TOKEN, DATA_DIR, DASHBOARDS_DIR, EXPORTS_DIR, LOGS_DIR, CONTEXT_MODE, HRV_API_URL
from hrv_calculator import HRVCalculator
from dashboard_generator import DashboardGenerator
from data_loader import DataLoader
from polar_connector import PolarConnector
from pmd_connector import PMDConnector

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOGS_DIR / 'bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def _resolve_latest_csv(user_id: int) -> Path | None:
    """Локальный CSV или скачать сырой замер с Railway api_server."""
    local = DATA_DIR / f"user_{user_id}_latest.csv"
    if local.exists():
        return local
    api = (HRV_API_URL or "").rstrip("/")
    if not api:
        return None
    try:
        import requests

        r = requests.get(f"{api}/api/v1/raw", params={"uid": user_id}, timeout=30)
        if r.status_code == 200 and r.content:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            local.write_bytes(r.content)
            return local
    except Exception as e:
        logger.warning(f"Railway /api/v1/raw failed: {e}")
    return None


class NeuroHRVBot:
    """Telegram бот для NeuroHRV Monitor"""
    
    def __init__(self):
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.data_loader = DataLoader()
        self.polar_connectors = {}  # user_id -> PolarConnector
        self.pmd_connectors = {}  # user_id -> PMDConnector
        self.user_context = {}  # user_id -> context mode
        self.last_scan_devices = {}  # user_id -> {address: name}
        self.setup_handlers()
    
    def setup_handlers(self):
        """Настройка обработчиков команд"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("analyze", self.analyze_command))
        self.application.add_handler(CommandHandler("export", self.export_command))
        # Команды для прямой работы с Polar
        self.application.add_handler(CommandHandler("scan", self.scan_polar_command))
        self.application.add_handler(CommandHandler("connect", self.connect_polar_command))
        self.application.add_handler(CommandHandler("start_measurement", self.start_measurement_command))
        self.application.add_handler(CommandHandler("stop_measurement", self.stop_measurement_command))
        self.application.add_handler(CommandHandler("disconnect", self.disconnect_polar_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("measure_phone", self.measure_phone_command))
        # Команды для PMD/SDK режима
        self.application.add_handler(CommandHandler("scan_pmd", self.scan_pmd_command))
        self.application.add_handler(CommandHandler("connect_pmd", self.connect_pmd_command))
        self.application.add_handler(CommandHandler("start_measurement_pmd", self.start_measurement_pmd_command))
        self.application.add_handler(CommandHandler("stop_measurement_pmd", self.stop_measurement_pmd_command))
        self.application.add_handler(CommandHandler("disconnect_pmd", self.disconnect_pmd_command))
        self.application.add_handler(CommandHandler("status_pmd", self.status_pmd_command))
        self.application.add_handler(CommandHandler("context", self.context_command))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        # Доп-команды (метрики/сегменты/xlsx/дашборд) — аддитивно, без правки BLE-логики
        try:
            import pulse_extras
            pulse_extras.register(self.application)
        except Exception as _e:
            logger.warning(f"pulse_extras не подключён: {_e}")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка команды /start"""
        welcome_text = (
            "👋 NeuroHRV Monitor — вариабельность сердечного ритма (HRV).\n"
            "Метрики калиброваны под Kubios.\n\n"
            "📱 Замер с телефона (Polar Verity + Web Bluetooth):\n"
            "/measure_phone — открыть замер в браузере.\n"
            "Меряешь, пока не нажмёшь «Стоп» (для метрик — 2–3 мин).\n\n"
            "📊 Результаты:\n"
            "/dashboard — ссылка на веб-дашборд (паутинка + метрики)\n"
            "/export — CSV с сырыми RR\n"
            "/export_xlsx — Excel с метриками и RR\n\n"
            "📄 Или пришли CSV-файл с колонкой RR_Interval_ms.\n"
            "/help — справка"
        )
        await update.message.reply_text(welcome_text)
    
    async def measure_phone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Замер через Bluetooth ТЕЛЕФОНА: открывает внешний Chrome с Web Bluetooth.
        Результат уходит на бэкенд (api_server), там калибруется и присылается в чат."""
        from config import HRV_WEBAPP_URL as webapp_url, HRV_API_URL as api_url
        user_id = update.effective_user.id
        if not webapp_url or not api_url:
            await update.message.reply_text(
                "⚠️ Телефонный замер не настроен.\n"
                "Нужны переменные окружения HRV_WEBAPP_URL (HTTPS-страница webapp) "
                "и HRV_API_URL (HTTPS-бэкенд api_server). Подними их по HTTPS "
                "(напр. cloudflared) и задай в .env."
            )
            return
        link = f"{webapp_url}/polar_webapp_fixed.html?uid={user_id}&api={api_url}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📱 Открыть замер в браузере", url=link)]])
        await update.message.reply_text(
            "📱 Замер с телефона (Bluetooth телефона, без ноутбука)\n\n"
            "1. Включи Polar Verity Sense.\n"
            "2. Жми кнопку — откроется ВНЕШНИЙ браузер (Chrome).\n"
            "3. «Подключить» → выбери Polar → «Начать стрим RR».\n"
            "4. Через 2–3 мин жми «Стоп» — калиброванный результат придёт сюда.\n\n"
            "⚠️ Нужен Android + Chrome (Web Bluetooth). iOS Safari не поддерживает.",
            reply_markup=kb
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка команды /help"""
        help_text = (
            "📚 Справка. Метрики калиброваны под Kubios.\n\n"
            "📱 Замер с телефона:\n"
            "/measure_phone — Chrome + Bluetooth телефона → Polar Verity.\n"
            "2–3 мин непрерывного RR для надёжных SDNN/RMSSD.\n\n"
            "📊 Результаты (веб-дашборд на Railway):\n"
            "/dashboard — открыть паутинку и метрики\n"
            "/export — скачать CSV с сырыми RR\n"
            "/export_xlsx — Excel: метрики + RR + сегменты\n"
            "/trends — динамика по сегментам · /mark — метка сегмента\n"
            "/metric <имя> — расшифровка метрики\n\n"
            "🎛 Контекст формулировок: /context cognitive|physical|universal\n\n"
            "📄 CSV-файл: колонка RR_Interval_ms (точно) или Heart_Rate_bpm (приближённо)."
        )
        await update.message.reply_text(help_text)
    
    async def analyze_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Устаревший путь: ведём на веб-дашборд Railway (без PNG neurohrv_*)."""
        user_id = update.effective_user.id
        api = (HRV_API_URL or "").rstrip("/")
        if not api:
            await update.message.reply_text(
                "⚠️ Дашборд не настроен (нет HRV_API_URL). Используй /dashboard."
            )
            return
        link = f"{api}/dashboard?uid={user_id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Открыть дашборд", url=link)]])
        await update.message.reply_text(
            "📊 Актуальный дашборд — паутинка, метрики и сводка по последнему замеру:\n"
            f"{link}\n\n"
            "Старые PNG-дашборды больше не генерируются — всё в веб-версии.",
            reply_markup=kb,
        )
    async def export_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """CSV с сырыми RR — локально или с Railway /api/v1/raw."""
        user_id = update.effective_user.id
        data_file = _resolve_latest_csv(user_id)

        if not data_file:
            await update.message.reply_text(
                "❌ Нет данных. Сначала /measure_phone или пришли CSV."
            )
            return

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(data_file, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"hrv_raw_{user_id}_{timestamp}.csv",
                    caption="📄 Сырые RR: Heart_Rate_bpm, Timestamp_ISO, RR_Interval_ms",
                )

        except Exception as e:
            logger.error(f"Ошибка при экспорте: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка при экспорте данных: {str(e)}")
    
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка загруженных документов"""
        document = update.message.document
        
        if not document.file_name.endswith('.csv'):
            await update.message.reply_text(
                "❌ Пожалуйста, отправьте CSV файл с данными HRV."
            )
            return
        
        await update.message.reply_text("📥 Загружаю файл...")
        
        try:
            # Скачивание файла
            file = await context.bot.get_file(document.file_id)
            user_id = update.effective_user.id
            data_file = DATA_DIR / f"user_{user_id}_latest.csv"
            
            await file.download_to_drive(data_file)
            
            # Проверка данных
            data = pd.read_csv(data_file)
            required_columns = ['Heart_Rate_bpm']
            
            if not all(col in data.columns for col in required_columns):
                await update.message.reply_text(
                    f"❌ Файл должен содержать колонки: {', '.join(required_columns)}"
                )
                return
            
            await update.message.reply_text(
                f"✅ Файл загружен! Найдено {len(data)} записей.\n\n"
                "Используйте /analyze для анализа данных."
            )
            
        except Exception as e:
            logger.error(f"Ошибка при загрузке файла: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Ошибка при загрузке файла: {str(e)}"
            )
    
    async def scan_polar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сканирование Polar устройств"""
        user_id = update.effective_user.id
        
        await update.message.reply_text("🔍 Сканирование Bluetooth устройств...")
        
        try:
            connector = PolarConnector()
            devices = await connector.scan_devices(timeout=10.0)
            
            if not devices:
                await update.message.reply_text(
                    "❌ Polar устройства не найдены.\n\n"
                    "💡 Убедитесь что:\n"
                    "• Устройство включено\n"
                    "• Bluetooth активен\n"
                    "• Устройство не подключено к другому устройству"
                )
                return
            
            text = "✅ Найдено Polar устройств:\n\n"
            self.last_scan_devices[user_id] = {}
            for i, device in enumerate(devices, 1):
                text += f"{i}. {device['name']}\n"
                text += f"   Адрес: {device['address']}\n"
                if device.get('rssi'):
                    text += f"   Сигнал: {device['rssi']} dBm\n"
                text += "\n"
                self.last_scan_devices[user_id][device['address']] = device['name']
            
            text += "Используйте /connect для подключения"
            await update.message.reply_text(text)
            
        except ImportError:
            await update.message.reply_text(
                "❌ Для работы с Polar требуется библиотека bleak.\n"
                "Установите: pip install bleak"
            )
        except Exception as e:
            logger.error(f"Ошибка при сканировании: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def connect_polar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подключение к Polar устройству"""
        user_id = update.effective_user.id
        
        # Проверяем, не подключен ли уже
        if user_id in self.polar_connectors:
            connector = self.polar_connectors[user_id]
            if connector.is_connected:
                await update.message.reply_text(
                    f"✅ Уже подключено к {connector.device_address}\n"
                    "Используйте /disconnect для отключения"
                )
                return
        
        device_address = None
        if context.args:
            device_address = context.args[0]
        
        await update.message.reply_text("🔌 Подключение к Polar устройству...")
        
        try:
            # Создаем callback для уведомления об отключении
            async def on_disconnect():
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="⚠️ Устройство Polar отключилось автоматически!\n\n"
                             "Возможные причины:\n"
                             "• Устройство выключилось\n"
                             "• Разрядилась батарея\n"
                             "• Потеря связи Bluetooth\n\n"
                             "Используйте /status для проверки состояния"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления об отключении: {e}")
            
            connector = PolarConnector(disconnect_callback=on_disconnect)
            success = await connector.connect(device_address)
            
            if success:
                name = None
                if user_id in self.last_scan_devices and connector.device_address in self.last_scan_devices[user_id]:
                    name = self.last_scan_devices[user_id][connector.device_address]
                connector.device_name = name
                self.polar_connectors[user_id] = connector
                await update.message.reply_text(
                    f"✅ Подключено к {connector.device_address}!\n\n"
                    "Используйте /start_measurement для начала измерения"
                )
            else:
                await update.message.reply_text(
                    "❌ Не удалось подключиться.\n"
                    "Попробуйте /scan для поиска устройств"
                )
        except Exception as e:
            logger.error(f"Ошибка при подключении: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def start_measurement_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало измерения HRV"""
        user_id = update.effective_user.id
        
        if user_id not in self.polar_connectors:
            await update.message.reply_text(
                "❌ Не подключено к Polar устройству.\n"
                "Используйте /connect для подключения"
            )
            return
        
        connector = self.polar_connectors[user_id]
        
        if not connector.is_connected:
            await update.message.reply_text(
                "❌ Устройство не подключено.\n"
                "Используйте /connect"
            )
            return

        if connector.is_streaming:
            await update.message.reply_text(
                "⚠️ Измерение уже идет. Дождитесь окончания или используйте /stop_measurement."
            )
            return
        
        duration = 180  # 3 минуты по умолчанию
        if context.args:
            try:
                duration = int(context.args[0])
            except ValueError:
                pass

        # PMD (PPI) — ПО УМОЛЧАНИЮ: у Polar Verity (оптика) стандартный HR-сервис
        # RR-интервалы не отдаёт вообще, точные интервалы идут только через PMD.
        # Принудительно обычный HR-режим: /start_measurement hr
        args_lower = [a.lower() for a in context.args] if context.args else []
        use_pmd = ("hr" not in args_lower)

        if use_pmd:
            try:
                await update.message.reply_text(
                    "ℹ️ Для Verity Sense запускаю PMD (PPI) — точные интервалы."
                )
                pmd = self.pmd_connectors.get(user_id)
                if not pmd:
                    async def on_disconnect():
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text="⚠️ PMD устройство отключилось автоматически."
                            )
                        except Exception as e:
                            logger.error(f"Ошибка PMD уведомления: {e}")
                    pmd = PMDConnector(disconnect_callback=on_disconnect)
                    self.pmd_connectors[user_id] = pmd
                if not pmd.is_connected:
                    # Verity допускает ОДНО BLE-подключение — освобождаем
                    # регулярный коннектор, иначе PMD не сможет подключиться.
                    dev_addr = connector.device_address
                    if connector.is_connected:
                        try:
                            await connector.disconnect()
                        except Exception:
                            pass
                    await pmd.connect(dev_addr)

                await update.message.reply_text(
                    f"📊 PMD: начало измерения на {duration} секунд...\n"
                    "Данные собираются..."
                )
                success = await pmd.start_streaming(duration_seconds=duration)
                if success:
                    await asyncio.sleep(duration + 5)
                    await pmd.stop_streaming()
                    data = pmd.get_dataframe()
                    if len(data) > 0:
                        self._save_data_files(data, user_id)
                        await update.message.reply_text(
                            f"✅ PMD измерение завершено!\n"
                            f"Собрано {len(data)} точек данных.\n\n"
                            "Используйте /analyze для получения дашбордов"
                        )
                        return
                    await update.message.reply_text("⚠️ Данные не собраны (PMD).")
                else:
                    await update.message.reply_text("❌ Не удалось начать PMD измерение")
            except ImportError:
                await update.message.reply_text("⚠️ PMD недоступен (нет bleakheart). Перехожу к обычному режиму.")
            except Exception as e:
                logger.error(f"Ошибка PMD измерения: {e}", exc_info=True)
                await update.message.reply_text("⚠️ PMD ошибка. Перехожу к обычному режиму.")
        
        await update.message.reply_text(
            f"📊 Начало измерения на {duration} секунд...\n"
            "Данные собираются..."
        )
        
        try:
            success = await connector.start_streaming(duration_seconds=duration)
            
            if success:
                # Ждем завершения измерения
                await asyncio.sleep(duration + 5)
                
                # Получаем данные
                data = connector.get_dataframe()
                
                if len(data) > 0:
                    # Сохраняем данные
                    self._save_data_files(data, user_id)

                    rr_source = data['RR_Source'].iloc[0] if 'RR_Source' in data.columns else 'unknown'
                    if rr_source == 'derived':
                        await update.message.reply_text(
                            "⚠️ Точные RR-интервалы не получены (PMD не поднялся, считаю из ЧСС — приближённо).\n"
                            "Проверь: датчик надет плотно и включён, это Polar Verity, "
                            "и он не подключён к другому устройству/приложению. Затем повтори /start_measurement."
                        )
                    
                    await update.message.reply_text(
                        f"✅ Измерение завершено!\n"
                        f"Собрано {len(data)} точек данных.\n\n"
                        "Используйте /analyze для получения дашбордов"
                    )
                else:
                    await update.message.reply_text(
                        "⚠️ Данные не собраны. Попробуйте еще раз."
                    )
            else:
                await update.message.reply_text("❌ Не удалось начать измерение")
                
        except Exception as e:
            logger.error(f"Ошибка при измерении: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def stop_measurement_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Остановка измерения"""
        user_id = update.effective_user.id
        
        if user_id not in self.polar_connectors:
            await update.message.reply_text("❌ Нет активного подключения")
            return
        
        connector = self.polar_connectors[user_id]
        
        try:
            await connector.stop_streaming()
            data = connector.get_dataframe()
            
            if len(data) > 0:
                self._save_data_files(data, user_id)
                
                await update.message.reply_text(
                    f"⏹️ Измерение остановлено.\n"
                    f"Собрано {len(data)} точек данных.\n\n"
                    "Используйте /analyze для анализа"
                )
            else:
                await update.message.reply_text("⏹️ Измерение остановлено")
                
        except Exception as e:
            logger.error(f"Ошибка при остановке: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def disconnect_polar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отключение от Polar устройства"""
        user_id = update.effective_user.id
        
        if user_id not in self.polar_connectors:
            await update.message.reply_text("❌ Нет активного подключения")
            return
        
        connector = self.polar_connectors[user_id]
        
        try:
            await connector.disconnect()
            del self.polar_connectors[user_id]
            await update.message.reply_text("🔌 Отключено от Polar устройства")
        except Exception as e:
            logger.error(f"Ошибка при отключении: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Статус подключения"""
        user_id = update.effective_user.id
        
        if user_id not in self.polar_connectors:
            await update.message.reply_text(
                "❌ Нет активного подключения\n"
                "Используйте /connect для подключения"
            )
            return
        
        connector = self.polar_connectors[user_id]
        status = connector.get_status()
        
        text = "📊 Статус подключения:\n\n"
        text += f"Подключено: {'✅' if status['connected'] else '❌'}\n"
        text += f"Измерение: {'📊 Активно' if status['streaming'] else '⏸️ Остановлено'}\n"
        text += f"Устройство: {status['device_address'] or 'Не выбрано'}\n"
        text += f"Точек данных: {status['data_points']}\n"
        text += f"Длительность: {int(status['duration_seconds'])} сек"
        
        await update.message.reply_text(text)

    async def scan_pmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сканирование Polar устройств (PMD)"""
        await update.message.reply_text("🔍 PMD: сканирование Bluetooth устройств...")
        try:
            connector = PMDConnector()
            devices = await connector.scan_devices(timeout=10.0)
            if not devices:
                await update.message.reply_text("❌ PMD устройства не найдены.")
                return
            text = "✅ Найдено PMD устройств:\n\n"
            for i, device in enumerate(devices, 1):
                text += f"{i}. {device['name']}\n"
                text += f"   Адрес: {device['address']}\n"
                if device.get('rssi'):
                    text += f"   Сигнал: {device['rssi']} dBm\n"
                text += "\n"
            text += "Используйте /connect_pmd для подключения"
            await update.message.reply_text(text)
        except ImportError:
            await update.message.reply_text(
                "❌ Для PMD режима требуется bleakheart.\n"
                "Установите: pip install bleakheart"
            )
        except Exception as e:
            logger.error(f"Ошибка PMD сканирования: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def connect_pmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подключение к Polar устройству (PMD)"""
        user_id = update.effective_user.id
        if user_id in self.pmd_connectors:
            connector = self.pmd_connectors[user_id]
            if connector.is_connected:
                await update.message.reply_text(
                    f"✅ Уже подключено (PMD) к {connector.device_address}\n"
                    "Используйте /disconnect_pmd для отключения"
                )
                return
        device_address = context.args[0] if context.args else None
        await update.message.reply_text("🔌 PMD: подключение к устройству...")
        try:
            async def on_disconnect():
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="⚠️ PMD устройство отключилось автоматически."
                    )
                except Exception as e:
                    logger.error(f"Ошибка PMD уведомления: {e}")
            connector = PMDConnector(disconnect_callback=on_disconnect)
            success = await connector.connect(device_address)
            if success:
                self.pmd_connectors[user_id] = connector
                await update.message.reply_text(
                    f"✅ PMD подключено к {connector.device_address}!\n\n"
                    "Используйте /start_measurement_pmd для начала измерения"
                )
            else:
                await update.message.reply_text("❌ Не удалось подключиться (PMD).")
        except Exception as e:
            logger.error(f"Ошибка PMD подключения: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def start_measurement_pmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало измерения HRV (PMD)"""
        user_id = update.effective_user.id
        if user_id not in self.pmd_connectors:
            await update.message.reply_text(
                "❌ Нет PMD подключения. Используйте /connect_pmd"
            )
            return
        connector = self.pmd_connectors[user_id]
        if not connector.is_connected:
            await update.message.reply_text("❌ PMD устройство не подключено.")
            return
        if connector.is_streaming:
            await update.message.reply_text("⚠️ PMD измерение уже идет.")
            return

        duration = 180
        enable_sdk_mode = False
        if context.args:
            for arg in context.args:
                if arg.lower() == "sdk":
                    enable_sdk_mode = True
            for arg in context.args:
                try:
                    duration = int(arg)
                    break
                except ValueError:
                    continue
        await update.message.reply_text(
            f"📊 PMD: начало измерения на {duration} секунд...\n"
            "Данные собираются..."
        )
        try:
            success = await connector.start_streaming(duration_seconds=duration, enable_sdk_mode=enable_sdk_mode)
            if success:
                await asyncio.sleep(duration + 5)
                # Гарантированно останавливаем поток после таймера
                await connector.stop_streaming()
                data = connector.get_dataframe()
                if len(data) > 0:
                    self._save_data_files(data, user_id)
                    await update.message.reply_text(
                        f"✅ PMD измерение завершено!\n"
                        f"Собрано {len(data)} точек данных.\n\n"
                        "Используйте /analyze для получения дашбордов"
                    )
                else:
                    await update.message.reply_text("⚠️ Данные не собраны (PMD).")
            else:
                await update.message.reply_text("❌ Не удалось начать PMD измерение")
        except Exception as e:
            logger.error(f"Ошибка PMD измерения: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def stop_measurement_pmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Остановка измерения (PMD)"""
        user_id = update.effective_user.id
        if user_id not in self.pmd_connectors:
            await update.message.reply_text("❌ Нет PMD подключения")
            return
        connector = self.pmd_connectors[user_id]
        try:
            await connector.stop_streaming()
            connector.is_streaming = False
            data = connector.get_dataframe()
            if len(data) > 0:
                self._save_data_files(data, user_id)
                await update.message.reply_text(
                    f"⏹️ PMD измерение остановлено.\n"
                    f"Собрано {len(data)} точек данных.\n\n"
                    "Используйте /analyze для анализа"
                )
            else:
                await update.message.reply_text("⏹️ PMD измерение остановлено")
        except Exception as e:
            logger.error(f"Ошибка PMD остановки: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def disconnect_pmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отключение от PMD устройства"""
        user_id = update.effective_user.id
        if user_id not in self.pmd_connectors:
            await update.message.reply_text("❌ Нет PMD подключения")
            return
        connector = self.pmd_connectors[user_id]
        try:
            await connector.disconnect()
            del self.pmd_connectors[user_id]
            await update.message.reply_text("🔌 PMD отключено")
        except Exception as e:
            logger.error(f"Ошибка PMD отключения: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def status_pmd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Статус PMD подключения"""
        user_id = update.effective_user.id
        if user_id not in self.pmd_connectors:
            await update.message.reply_text("❌ Нет PMD подключения")
            return
        connector = self.pmd_connectors[user_id]
        status = connector.get_status()
        text = "📊 PMD статус:\n\n"
        text += f"Подключено: {'✅' if status['connected'] else '❌'}\n"
        text += f"Измерение: {'📊 Активно' if status['streaming'] else '⏸️ Остановлено'}\n"
        text += f"Устройство: {status['device_address'] or 'Не выбрано'}\n"
        text += f"Точек данных: {status['data_points']}\n"
        text += f"Длительность: {int(status['duration_seconds'])} сек"
        await update.message.reply_text(text)

    async def context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор контекста формулировок"""
        user_id = update.effective_user.id
        if not context.args:
            current = self.user_context.get(user_id, CONTEXT_MODE)
            await update.message.reply_text(
                f"Текущий контекст: {current}\n"
                "Доступные: cognitive | physical | universal"
            )
            return
        mode = context.args[0].lower()
        if mode not in ("cognitive", "physical", "universal"):
            await update.message.reply_text("Неверный контекст. Используйте: cognitive | physical | universal")
            return
        self.user_context[user_id] = mode
        await update.message.reply_text(f"Контекст установлен: {mode}")
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка callback запросов"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text="Обработка...")

    def _save_data_files(self, data: pd.DataFrame, user_id: int):
        """Сохранение сырых данных + 1Hz версии"""
        data_file = DATA_DIR / f"user_{user_id}_latest.csv"
        data.to_csv(data_file, index=False)

        if 'Timestamp_ISO' in data.columns:
            try:
                df = data.copy()
                df['Timestamp_ISO'] = pd.to_datetime(df['Timestamp_ISO'], errors='coerce')
                df = df.dropna(subset=['Timestamp_ISO']).set_index('Timestamp_ISO')
                resampled = df.resample('1S').mean(numeric_only=True).interpolate()
                resampled = resampled.reset_index()
                resampled_file = DATA_DIR / f"user_{user_id}_latest_1hz.csv"
                resampled.to_csv(resampled_file, index=False)
            except Exception as e:
                logger.warning(f"Не удалось сохранить 1Hz CSV: {e}")
    
    def run(self):
        """Запуск бота"""
        logger.info("Запуск бота NeuroHRV Monitor...")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    """Главная функция"""
    bot = NeuroHRVBot()
    bot.run()


if __name__ == "__main__":
    main()
