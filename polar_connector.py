"""
Прямое подключение к Polar устройству через Bluetooth
Не требует Android приложения - только Telegram бот!
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Callable
from collections import deque
import numpy as np
import pandas as pd

try:
    from bleak import BleakScanner, BleakClient
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False

logger = logging.getLogger(__name__)

# Polar PMD Service UUIDs
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# Настройки
SAMPLE_RATE = 135  # Hz для Polar Verity Sense
MIN_DURATION_SEC = 60  # Минимум 1 минута для анализа


class PolarConnector:
    """Прямое подключение к Polar устройству через Bluetooth"""
    
    def __init__(self, data_callback: Optional[Callable] = None, disconnect_callback: Optional[Callable] = None):
        """
        Инициализация подключения к Polar
        
        Args:
            data_callback: Функция обратного вызова для обработки данных HR
            disconnect_callback: Функция обратного вызова при отключении устройства
        """
        if not BLEAK_AVAILABLE:
            raise ImportError(
                "Для работы с Polar требуется библиотека bleak. "
                "Установите: pip install bleak"
            )
        
        self.data_callback = data_callback
        self.disconnect_callback = disconnect_callback
        self.client: Optional[BleakClient] = None
        self.device_address: Optional[str] = None
        self.device_name: Optional[str] = None
        self.is_connected = False
        self.is_streaming = False
        
        # Буферы данных
        self.hr_data: List[float] = []
        self.timestamps: List[str] = []
        self.rr_intervals: List[float] = []
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self._rr_from_device = False
        
        logger.info("✅ Инициализирован Polar Connector")
    
    def _on_disconnect(self):
        """Обработчик автоматического отключения устройства"""
        logger.warning("⚠️ Устройство отключилось автоматически!")
        self.is_connected = False
        self.is_streaming = False
        
        # Вызываем callback если есть
        if self.disconnect_callback:
            try:
                self.disconnect_callback()
            except Exception as e:
                logger.error(f"Ошибка в disconnect_callback: {e}")
    
    async def scan_devices(self, timeout: float = 10.0) -> List[dict]:
        """
        Сканирование Bluetooth устройств для поиска Polar
        
        Args:
            timeout: Время сканирования в секундах
        
        Returns:
            Список найденных Polar устройств
        """
        if not BLEAK_AVAILABLE:
            return []
        
        logger.info("🔍 Сканирование Bluetooth устройств...")
        
        try:
            devices = await BleakScanner.discover(timeout=timeout)
            polar_devices = []
            
            for device in devices:
                name = device.name or ""
                if "Polar" in name or "Verity" in name or "H10" in name:
                    polar_devices.append({
                        'name': name,
                        'address': device.address,
                        'rssi': device.rssi if hasattr(device, 'rssi') else None
                    })
                    logger.info(f"  ✓ Найдено: {name} ({device.address})")
            
            if not polar_devices:
                logger.warning("⚠️ Polar устройства не найдены")
            
            return polar_devices
            
        except Exception as e:
            logger.error(f"❌ Ошибка при сканировании: {e}")
            return []
    
    async def connect(self, device_address: Optional[str] = None) -> bool:
        """
        Подключение к Polar устройству
        
        Args:
            device_address: MAC адрес устройства (если None, будет поиск)
        
        Returns:
            True если подключение успешно
        """
        if not BLEAK_AVAILABLE:
            logger.error("❌ Библиотека bleak не установлена")
            return False
        
        try:
            # Если адрес не указан, ищем устройство
            if not device_address:
                devices = await self.scan_devices()
                if not devices:
                    logger.error("❌ Polar устройства не найдены")
                    return False
                device_address = devices[0]['address']
            
            self.device_address = device_address
            logger.info(f"🔌 Подключение к {device_address}...")
            
            # Создаем клиент с обработчиком отключения
            self.client = BleakClient(
                device_address,
                disconnected_callback=self._on_disconnect
            )
            await self.client.connect()
            
            if self.client.is_connected:
                self.is_connected = True
                self.start_time = datetime.now()
                logger.info("✅ Подключено к Polar устройству")
                return True
            else:
                logger.error("❌ Не удалось подключиться")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка подключения: {e}")
            return False
    
    async def disconnect(self):
        """Отключение от устройства"""
        if self.client and self.is_connected:
            try:
                if self.is_streaming:
                    await self.stop_streaming()
                await self.client.disconnect()
                self.is_connected = False
                logger.info("🔌 Отключено от Polar устройства")
            except Exception as e:
                logger.error(f"❌ Ошибка при отключении: {e}")
    
    async def start_streaming(self, duration_seconds: int = 300) -> bool:
        """
        Начало потока данных HR
        
        Args:
            duration_seconds: Длительность записи в секундах
        
        Returns:
            True если поток начат успешно
        """
        if not self.is_connected:
            logger.error("❌ Не подключено к устройству")
            return False
        
        try:
            # Очищаем буферы
            self.hr_data = []
            self.timestamps = []
            self.rr_intervals = []
            self.start_time = datetime.now()
            self.end_time = None
            self._rr_from_device = False
            
            # Подписываемся на уведомления HR
            # Для Polar Verity Sense используем Heart Rate Service
            hr_service_uuid = "0000180d-0000-1000-8000-00805f9b34fb"
            hr_measurement_uuid = "00002a37-0000-1000-8000-00805f9b34fb"
            
            await self.client.start_notify(
                hr_measurement_uuid,
                self._handle_hr_data
            )
            
            self.is_streaming = True
            logger.info(f"📊 Начат поток данных HR на {duration_seconds} секунд")
            
            # Запускаем таймер для остановки
            asyncio.create_task(self._stream_timer(duration_seconds))
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка при запуске потока: {e}")
            return False
    
    def _handle_hr_data(self, sender, data: bytearray):
        """
        Обработка данных HR от устройства
        
        Args:
            sender: UUID характеристики
            data: Данные в формате bytearray
        """
        # Проверяем что устройство еще подключено
        if not self.is_connected or (self.client and not self.client.is_connected):
            logger.warning("⚠️ Попытка получить данные от отключенного устройства")
            self._on_disconnect()
            return
        
        try:
            # Парсинг данных HR согласно Bluetooth Heart Rate Profile
            # Формат: [Flags, HR Value, ...]
            if len(data) < 2:
                return
            
            flags = data[0]
            idx = 1
            hr_value = data[idx]
            
            # Если есть 16-bit значение
            if flags & 0x01:
                if len(data) >= idx + 2:
                    hr_value = int.from_bytes(data[idx:idx+2], byteorder='little')
                    idx += 2
            else:
                idx += 1

            # Пропускаем Energy Expended (если присутствует)
            if flags & 0x08:
                if len(data) >= idx + 2:
                    idx += 2

            # RR-Interval (если присутствует) — значения в 1/1024 сек
            if flags & 0x10:
                while idx + 1 < len(data):
                    rr_raw = int.from_bytes(data[idx:idx+2], byteorder='little')
                    rr_ms = rr_raw * 1000.0 / 1024.0
                    self.rr_intervals.append(rr_ms)
                    self._rr_from_device = True
                    idx += 2
            
            # Добавляем данные
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.hr_data.append(float(hr_value))
            self.timestamps.append(timestamp)
            
            # Вызываем callback если есть
            if self.data_callback:
                self.data_callback(hr_value, timestamp)
            
            logger.debug(f"💓 HR: {hr_value} уд/мин")
            
        except Exception as e:
            logger.error(f"❌ Ошибка обработки HR данных: {e}")
    
    async def _stream_timer(self, duration_seconds: int):
        """Таймер для автоматической остановки потока"""
        await asyncio.sleep(duration_seconds)
        if self.is_streaming:
            await self.stop_streaming()
            logger.info(f"⏱️ Запись завершена ({duration_seconds} секунд)")
    
    async def stop_streaming(self):
        """Остановка потока данных"""
        if not self.is_streaming:
            return
        
        try:
            hr_measurement_uuid = "00002a37-0000-1000-8000-00805f9b34fb"
            await self.client.stop_notify(hr_measurement_uuid)
            self.is_streaming = False
            self.end_time = datetime.now()
            logger.info("⏹️ Поток данных остановлен")
        except Exception as e:
            logger.error(f"❌ Ошибка при остановке потока: {e}")
    
    def get_dataframe(self) -> pd.DataFrame:
        """
        Получение собранных данных в виде DataFrame
        
        Returns:
            DataFrame с колонками Heart_Rate_bpm и Timestamp_ISO
        """
        duration_s = 0.0
        if self.start_time and self.end_time:
            duration_s = (self.end_time - self.start_time).total_seconds()
        elif self.rr_intervals:
            duration_s = float(np.sum(self.rr_intervals) / 1000.0)
        elif self.timestamps:
            try:
                start_ts = datetime.fromisoformat(self.timestamps[0])
                end_ts = datetime.fromisoformat(self.timestamps[-1])
                duration_s = (end_ts - start_ts).total_seconds()
            except Exception:
                duration_s = 0.0

        if self.rr_intervals:
            base_time = self.start_time or datetime.now()
            cum_seconds = np.cumsum(self.rr_intervals) / 1000.0
            timestamps = [
                (base_time + timedelta(seconds=float(s))).strftime("%Y-%m-%d %H:%M:%S")
                for s in cum_seconds
            ]
            hr_from_rr = [60000.0 / rr for rr in self.rr_intervals]
            data = pd.DataFrame({
                'Heart_Rate_bpm': hr_from_rr,
                'Timestamp_ISO': timestamps,
                'RR_Interval_ms': self.rr_intervals,
                'Second': range(len(self.rr_intervals)),
                'RR_Source': 'device' if self._rr_from_device else 'device',
                'Duration_Seconds': duration_s
            })
            return data
        
        if not self.hr_data:
            return pd.DataFrame()
        
        data = pd.DataFrame({
            'Heart_Rate_bpm': self.hr_data,
            'Timestamp_ISO': self.timestamps
        })
        
        # Добавляем расчетные поля
        data['RR_Interval_ms'] = 60000.0 / data['Heart_Rate_bpm']
        data['Second'] = range(len(data))
        data['RR_Source'] = 'derived'
        data['Duration_Seconds'] = duration_s
        
        return data
    
    def get_status(self) -> dict:
        """Получение статуса подключения"""
        return {
            'connected': self.is_connected,
            'streaming': self.is_streaming,
            'device_address': self.device_address,
            'data_points': len(self.hr_data),
            'duration_seconds': (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        }
