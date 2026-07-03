"""
Модуль загрузки данных HRV
Поддерживает различные источники: CSV файлы, Android приложение, API
"""
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
import json
try:
    import requests
except ImportError:
    requests = None


class DataLoader:
    """Загрузчик данных HRV"""
    
    def __init__(self, data_dir: Optional[Path] = None):
        """
        Инициализация загрузчика
        
        Args:
            data_dir: Директория с данными (опционально)
        """
        self.data_dir = data_dir
    
    def load_from_csv(self, filepath: str) -> pd.DataFrame:
        """
        Загрузка данных из CSV файла
        
        Args:
            filepath: Путь к CSV файлу
        
        Returns:
            DataFrame с данными
        """
        data = pd.read_csv(filepath)
        
        # Проверка обязательных колонок
        if 'Heart_Rate_bpm' not in data.columns:
            raise ValueError("CSV файл должен содержать колонку 'Heart_Rate_bpm'")
        
        # Если нет RR интервалов, рассчитываем их
        if 'RR_Interval_ms' not in data.columns:
            data['RR_Interval_ms'] = 60000.0 / data['Heart_Rate_bpm']
        
        # Если нет временных меток, создаем их
        if 'Timestamp_ISO' not in data.columns:
            if 'Second' in data.columns:
                # Используем колонку Second для создания временных меток
                start_time = datetime.now()
                data['Timestamp_ISO'] = [
                    (start_time + pd.Timedelta(seconds=int(s))).strftime("%Y-%m-%d %H:%M:%S")
                    for s in data['Second']
                ]
            else:
                # Создаем последовательные временные метки
                start_time = datetime.now()
                data['Timestamp_ISO'] = [
                    (start_time + pd.Timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
                    for i in range(len(data))
                ]
        
        # Если нет приближенных метрик, создаем заглушки
        if 'Approximate_RMSSD' not in data.columns:
            data['Approximate_RMSSD'] = np.nan
        if 'Approximate_SDNN' not in data.columns:
            data['Approximate_SDNN'] = np.nan
        if 'Approximate_Stress_Index' not in data.columns:
            data['Approximate_Stress_Index'] = np.nan
        
        return data
    
    def load_from_android(self, android_path: str = None) -> pd.DataFrame:
        """
        Загрузка данных из Android приложения
        Android приложение сохраняет CSV файлы в локальную папку android_sync
        
        Args:
            android_path: Путь к файлу данных (опционально, ищет последний файл)
        
        Returns:
            DataFrame с данными
        """
        from config import LOCAL_ANDROID_SYNC_PATH
        
        # Создаем папку если её нет
        LOCAL_ANDROID_SYNC_PATH.mkdir(parents=True, exist_ok=True)
        
        if android_path:
            # Загружаем конкретный файл
            filepath = Path(android_path)
        else:
            # Ищем последний CSV файл в папке android_sync
            csv_files = list(LOCAL_ANDROID_SYNC_PATH.glob("*.csv"))
            if not csv_files:
                raise FileNotFoundError(
                    f"Не найдены CSV файлы в {LOCAL_ANDROID_SYNC_PATH}. "
                    "Убедитесь, что Android приложение сохранило данные."
                )
            # Берем самый новый файл
            filepath = max(csv_files, key=lambda p: p.stat().st_mtime)
        
        if not filepath.exists():
            raise FileNotFoundError(f"Файл не найден: {filepath}")
        
        # Загружаем через стандартный метод
        return self.load_from_csv(str(filepath))
    
    def load_from_api(self, api_url: str, api_key: Optional[str] = None) -> pd.DataFrame:
        """
        Загрузка данных через API
        
        Args:
            api_url: URL API endpoint
            api_key: API ключ (опционально)
        
        Returns:
            DataFrame с данными
        """
        if requests is None:
            raise ImportError(
                "Для работы с API требуется библиотека requests. "
                "Установите: pip install requests"
            )
        
        try:
            headers = {}
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'
            
            response = requests.get(api_url, headers=headers, timeout=10)
            response.raise_for_status()
            
            # Предполагаем, что API возвращает JSON с данными
            data_json = response.json()
            
            # Конвертируем в DataFrame
            if isinstance(data_json, list):
                data = pd.DataFrame(data_json)
            elif isinstance(data_json, dict) and 'data' in data_json:
                data = pd.DataFrame(data_json['data'])
            else:
                data = pd.DataFrame([data_json])
            
            # Проверяем обязательные колонки
            if 'Heart_Rate_bpm' not in data.columns:
                raise ValueError("API не вернул колонку 'Heart_Rate_bpm'")
            
            # Обрабатываем как обычный CSV
            if 'RR_Interval_ms' not in data.columns:
                data['RR_Interval_ms'] = 60000.0 / data['Heart_Rate_bpm']
            
            if 'Timestamp_ISO' not in data.columns:
                start_time = datetime.now()
                data['Timestamp_ISO'] = [
                    (start_time + pd.Timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
                    for i in range(len(data))
                ]
            
            return data
            
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Ошибка при подключении к API: {e}")
        except Exception as e:
            raise ValueError(f"Ошибка при обработке данных API: {e}")
    
    def validate_data(self, data: pd.DataFrame) -> Dict[str, bool]:
        """
        Валидация данных
        
        Args:
            data: DataFrame с данными
        
        Returns:
            Словарь с результатами валидации
        """
        results = {
            'has_hr': 'Heart_Rate_bpm' in data.columns,
            'has_timestamps': 'Timestamp_ISO' in data.columns,
            'has_min_records': len(data) >= 180,  # Минимум 3 минуты для анализа
            'hr_in_range': True,
            'no_nulls': True
        }
        
        if results['has_hr']:
            hr_values = data['Heart_Rate_bpm'].values
            results['hr_in_range'] = np.all((hr_values >= 30) & (hr_values <= 200))
            results['no_nulls'] = not data['Heart_Rate_bpm'].isnull().any()
        
        return results
    
    def prepare_sample_data(self, duration_seconds: int = 300) -> pd.DataFrame:
        """
        Создание тестовых данных для демонстрации
        
        Args:
            duration_seconds: Длительность записи в секундах
        
        Returns:
            DataFrame с тестовыми данными
        """
        # Генерация тестовых данных HR
        np.random.seed(42)
        base_hr = 75
        hr_data = base_hr + np.random.normal(0, 3, duration_seconds)
        hr_data = np.clip(hr_data, 50, 100)
        
        # Создание временных меток
        start_time = datetime.now()
        timestamps = [
            (start_time + pd.Timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
            for i in range(duration_seconds)
        ]
        
        # Расчет RR интервалов
        rr_intervals = 60000.0 / hr_data
        
        # Создание DataFrame
        data = pd.DataFrame({
            'Second': range(duration_seconds),
            'Timestamp_ISO': timestamps,
            'Heart_Rate_bpm': hr_data,
            'RR_Interval_ms': rr_intervals,
            'Approximate_RMSSD': np.nan,
            'Approximate_SDNN': np.nan,
            'Approximate_Stress_Index': np.nan
        })
        
        return data
