"""
PMD/SDK подключение к Polar устройствам через bleakheart.
Поддерживает получение PPI (RR) и HR через PMD.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Callable

import numpy as np
import pandas as pd

try:
    from bleak import BleakScanner, BleakClient
    from bleakheart import HeartRate, PolarMeasurementData
    BLEAKHEART_AVAILABLE = True
except ImportError:
    BLEAKHEART_AVAILABLE = False

logger = logging.getLogger(__name__)


class PMDConnector:
    """PMD/SDK подключение через bleakheart (ECG/PPG/PPI)."""

    def __init__(self, data_callback: Optional[Callable] = None, disconnect_callback: Optional[Callable] = None):
        if not BLEAKHEART_AVAILABLE:
            raise ImportError(
                "Для PMD режима требуется bleakheart. Установите: pip install bleakheart"
            )

        self.data_callback = data_callback
        self.disconnect_callback = disconnect_callback
        self.client: Optional[BleakClient] = None
        self.device_address: Optional[str] = None
        self.is_connected = False
        self.is_streaming = False

        self.hr_data: List[float] = []
        self.rr_intervals: List[float] = []
        self.timestamps: List[str] = []
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self._rr_elapsed_ms: float = 0.0

        self._hr_service: Optional[HeartRate] = None
        self._pmd: Optional[PolarMeasurementData] = None

    def _on_disconnect(self):
        self.is_connected = False
        self.is_streaming = False
        if self.disconnect_callback:
            try:
                self.disconnect_callback()
            except Exception as e:
                logger.error(f"Ошибка в disconnect_callback: {e}")

    async def scan_devices(self, timeout: float = 10.0) -> List[dict]:
        if not BLEAKHEART_AVAILABLE:
            return []
        try:
            devices = await BleakScanner.discover(timeout=timeout)
            polar_devices = []
            for device in devices:
                name = device.name or ""
                if "Polar" in name or "H10" in name or "Verity" in name:
                    polar_devices.append({
                        'name': name,
                        'address': device.address,
                        'rssi': device.rssi if hasattr(device, 'rssi') else None
                    })
            return polar_devices
        except Exception as e:
            logger.error(f"Ошибка при сканировании PMD: {e}")
            return []

    async def connect(self, device_address: Optional[str] = None) -> bool:
        if not BLEAKHEART_AVAILABLE:
            return False
        try:
            if not device_address:
                devices = await self.scan_devices()
                if not devices:
                    return False
                device_address = devices[0]['address']

            self.device_address = device_address
            self.client = BleakClient(device_address, disconnected_callback=self._on_disconnect)
            await self.client.connect()

            if self.client.is_connected:
                self.is_connected = True
                self.start_time = datetime.now()
                # В PMD режиме используем только PPI, чтобы не смешивать источники RR
                self._hr_service = HeartRate(self.client, callback=self._handle_hr_frame, unpack=True, instant_rate=True)
                self._pmd = PolarMeasurementData(self.client, callback=self._handle_pmd_frame)
                return True
            return False
        except Exception as e:
            logger.error(f"Ошибка PMD подключения: {e}")
            return False

    async def disconnect(self):
        if self.client and self.is_connected:
            try:
                if self.is_streaming:
                    await self.stop_streaming()
                await self.client.disconnect()
                self.is_connected = False
            except Exception as e:
                logger.error(f"Ошибка при отключении PMD: {e}")

    async def start_streaming(self, duration_seconds: int = 180, enable_sdk_mode: bool = False) -> bool:
        if not self.is_connected or not self.client:
            return False

        try:
            self.hr_data = []
            self.rr_intervals = []
            self.timestamps = []
            self.start_time = datetime.now()
            self.end_time = None
            self._rr_elapsed_ms = 0.0

            # В PMD режиме НЕ стартуем HR-сервис, чтобы не смешивать RR из разных источников

            if self._pmd:
                if enable_sdk_mode:
                    await self._pmd.start_streaming('SDK')
                await self._pmd.start_streaming('PPI')

            self.is_streaming = True
            asyncio.create_task(self._stream_timer(duration_seconds))
            return True
        except Exception as e:
            logger.error(f"Ошибка PMD старта потока: {e}")
            return False

    async def stop_streaming(self):
        if not self.is_streaming:
            return
        try:
            self.end_time = datetime.now()
            if self._pmd:
                await self._pmd.stop_streaming('PPI')
            if self._hr_service:
                await self._hr_service.stop_notify()
        except Exception as e:
            logger.error(f"Ошибка PMD остановки потока: {e}")
        finally:
            self.is_streaming = False

    async def _stream_timer(self, duration_seconds: int):
        await asyncio.sleep(duration_seconds)
        if self.is_streaming:
            await self.stop_streaming()

    def _handle_hr_frame(self, payload):
        try:
            _, tstamp_ns, data, _energy = payload
            hr, rr = data
            # HR-сервис используется только как резервный источник, не для PMD измерений
            if hr is not None:
                self.hr_data.append(float(hr))
            if rr is not None:
                self.rr_intervals.append(float(rr))
            if self.start_time:
                self.timestamps.append(datetime.fromtimestamp(tstamp_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            logger.error(f"Ошибка обработки HR frame: {e}")

    def _handle_pmd_frame(self, payload):
        try:
            meas, tstamp_ns, data = payload
            if meas != 'PPI':
                return
            buf = bytes(data)

            frame_type = None
            offset = None
            # Формат PMD PPI: [frame_type][8B timestamp][samples...]
            if len(buf) >= 9 and buf[0] in (0x00, 0x01):
                frame_type = buf[0]
                offset = 1 + 8
            elif len(buf) >= 10 and buf[9] in (0x00, 0x01):
                # Фоллбек для старого смещения
                frame_type = buf[9]
                offset = 10
            elif len(buf) % 6 == 0:
                # Фрейм без заголовка
                frame_type = 0x00
                offset = 0

            if frame_type != 0x00 or offset is None or offset >= len(buf):
                return

            samples = []
            for i in range(offset, len(buf), 6):
                if i + 5 >= len(buf):
                    break
                hr = buf[i]
                ppi_ms = int.from_bytes(buf[i+1:i+3], 'little', signed=False)
                err_ms = int.from_bytes(buf[i+3:i+5], 'little', signed=False)
                _flags = buf[i+5]
                # Фильтр качества: err_ms >= 30 обычно указывает на артефакты движения
                if err_ms >= 30:
                    continue
                if 300 <= ppi_ms <= 2000:
                    samples.append((float(hr), float(ppi_ms)))

            if not samples:
                return

            # Временные метки
            if self.start_time is None:
                self.start_time = datetime.now()

            def _is_epoch_ns(value: int) -> bool:
                sec = value / 1e9
                return 946684800 <= sec <= 4102444800  # 2000-01-01..2100-01-01

            timestamps = []
            if tstamp_ns and _is_epoch_ns(int(tstamp_ns)):
                frame_end = datetime.fromtimestamp(tstamp_ns / 1e9)
                total_ms = 0.0
                for hr, ppi_ms in reversed(samples):
                    total_ms += ppi_ms
                    timestamps.append(frame_end - timedelta(milliseconds=total_ms))
                timestamps = list(reversed(timestamps))
            else:
                for hr, ppi_ms in samples:
                    ts = self.start_time + timedelta(milliseconds=self._rr_elapsed_ms)
                    timestamps.append(ts)
                    self._rr_elapsed_ms += ppi_ms

            for (hr, ppi_ms), ts in zip(samples, timestamps):
                self.rr_intervals.append(ppi_ms)
                self.hr_data.append(hr)
                self.timestamps.append(ts.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            logger.error(f"Ошибка обработки PPI: {e}")

    def get_dataframe(self) -> pd.DataFrame:
        if self.rr_intervals:
            if len(self.timestamps) == len(self.rr_intervals):
                timestamps = self.timestamps
            else:
                base_time = self.start_time or datetime.now()
                cum_seconds = np.cumsum(self.rr_intervals) / 1000.0
                timestamps = [
                    (base_time + timedelta(seconds=float(s))).strftime("%Y-%m-%d %H:%M:%S")
                    for s in cum_seconds
                ]
            if len(self.hr_data) == len(self.rr_intervals) and np.max(self.hr_data) > 0:
                hr_from_rr = self.hr_data
            else:
                hr_from_rr = [60000.0 / rr for rr in self.rr_intervals]
            duration_s = 0.0
            if self.start_time and self.end_time:
                duration_s = (self.end_time - self.start_time).total_seconds()
            else:
                duration_s = float(np.sum(self.rr_intervals) / 1000.0)
            data = pd.DataFrame({
                'Heart_Rate_bpm': hr_from_rr,
                'Timestamp_ISO': timestamps,
                'RR_Interval_ms': self.rr_intervals,
                'Second': range(len(self.rr_intervals)),
                'RR_Source': 'pmd',
                'Duration_Seconds': duration_s
            })
            # Сортируем по времени, чтобы графики не "ломались"
            data = data.sort_values('Timestamp_ISO').reset_index(drop=True)
            data['Second'] = range(len(data))
            return data

        if not self.hr_data:
            return pd.DataFrame()

        data = pd.DataFrame({
            'Heart_Rate_bpm': self.hr_data,
            'Timestamp_ISO': self.timestamps
        })
        data['RR_Interval_ms'] = 60000.0 / data['Heart_Rate_bpm']
        data['Second'] = range(len(data))
        return data

    def get_status(self) -> dict:
        return {
            'connected': self.is_connected,
            'streaming': self.is_streaming,
            'device_address': self.device_address,
            'data_points': len(self.hr_data),
            'duration_seconds': (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        }
