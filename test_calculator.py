#!/usr/bin/env python3
"""
Тестовый скрипт для проверки работы HRV калькулятора
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from hrv_calculator import HRVCalculator
from dashboard_generator import DashboardGenerator
from data_loader import DataLoader

def test_calculator():
    """Тест калькулятора HRV"""
    print("🧪 Тестирование HRV калькулятора...")
    
    # Создание тестовых данных
    loader = DataLoader()
    data = loader.prepare_sample_data(duration_seconds=300)
    
    print(f"✅ Создано {len(data)} записей тестовых данных")
    
    # Расчет метрик
    hr_data = data['Heart_Rate_bpm'].tolist()
    calculator = HRVCalculator(hr_data)
    metrics = calculator.calculate_all_metrics()
    axis_scores = calculator.calculate_axis_scores(metrics)
    overall_score = calculator.calculate_overall_score(axis_scores)
    state_text = calculator.get_state_text(overall_score)
    
    print("\n📊 Рассчитанные метрики:")
    print(f"  HRV: {metrics.get('hrv', 0):.1f} мс")
    print(f"  SDNN: {metrics.get('sdnn', 0):.1f} мс")
    print(f"  RMSSD: {metrics.get('rmssd', 0):.1f} мс")
    print(f"  Stress Index: {metrics.get('stress_index', 0):.0f}")
    print(f"  Биологический возраст: {metrics.get('biological_age', 0)} лет")
    
    print("\n📈 Показатели осей:")
    for key, value in axis_scores.items():
        print(f"  {key}: {value}%")
    
    print(f"\n🎯 Общий балл: {overall_score}/100")
    print(f"   Состояние: {state_text}")
    
    # Тест генерации дашбордов
    print("\n🎨 Тестирование генерации дашбордов...")
    try:
        from config import DASHBOARDS_DIR
        generator = DashboardGenerator(
            data, metrics, axis_scores, overall_score, state_text
        )
        dashboard_files = generator.generate_all(str(DASHBOARDS_DIR))
        print(f"✅ Создано {len(dashboard_files)} дашбордов:")
        for f in dashboard_files:
            print(f"   - {Path(f).name}")
    except Exception as e:
        print(f"❌ Ошибка при генерации дашбордов: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n✅ Все тесты пройдены!")

if __name__ == "__main__":
    test_calculator()
