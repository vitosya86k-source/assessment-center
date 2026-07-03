"""
Модуль расчета HRV показателей из HR данных
Учитывает, что RR интервалы рассчитываются из HR (приближенные данные)
"""
import numpy as np
from scipy import signal
from scipy.interpolate import CubicSpline
from typing import Dict, List, Tuple
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Калибровка SDNN под Kubios + индексы PNS/SNS (тот же расчёт, что в auto_collector.py).
# Раньше калибровка в боте НЕ применялась — теперь применяется здесь.
try:
    from hrv_calibration import compute_sdnn_calibrated, calibration_info
    from kubios_indices import compute_pns_index, compute_sns_index
    _CALIB_OK = True
except Exception as _e:  # окружение без neurokit2/калибровки — работаем как раньше
    _CALIB_OK = False
    logger.warning(f"Калибровка/Kubios-индексы недоступны, используем сырой SDNN: {_e}")


class HRVCalculator:
    """
    Калькулятор HRV показателей из HR данных
    Важно: RR интервалы рассчитываются из HR, поэтому данные приближенные
    """
    
    def __init__(self, hr_data: List[float], timestamps: List[str] = None, rr_intervals: List[float] = None,
                 artifact_correction_level: str = "medium"):
        """
        Инициализация калькулятора
        
        Args:
            hr_data: Список значений HR (уд/мин)
            timestamps: Список временных меток (опционально)
        """
        hr_arr = np.array(hr_data, dtype=float)
        hr_clean = hr_arr[~np.isnan(hr_arr) & (hr_arr > 0)]
        self.hr_data = hr_clean
        self.timestamps = timestamps if timestamps else None
        
        if rr_intervals is not None:
            rr_arr = np.array(rr_intervals, dtype=float)
            rr_clean = rr_arr[~np.isnan(rr_arr) & (rr_arr > 0)]
            self.rr_intervals = rr_clean
        else:
            # Рассчитываем RR интервалы из HR
            self.rr_intervals = self._calculate_rr_from_hr(hr_data)
        self.artifact_correction_level = artifact_correction_level
        
    def _calculate_rr_from_hr(self, hr_data: List[float]) -> np.ndarray:
        """
        Расчет RR интервалов из HR
        RR (мс) = 60000 / HR (уд/мин)
        Удаляет NaN значения
        """
        hr_arr = np.array(hr_data, dtype=float)
        # Удаляем NaN и нулевые значения
        hr_clean = hr_arr[~np.isnan(hr_arr) & (hr_arr > 0)]
        if len(hr_clean) == 0:
            logger.warning("No valid HR data after filtering NaN")
            return np.array([])
        rr = 60000.0 / hr_clean
        return rr
    
    def calculate_all_metrics(self) -> Dict:
        """
        Расчет всех HRV метрик
        
        Returns:
            Словарь со всеми рассчитанными метриками
        """
        rr = self._clean_rr_artifacts(self.rr_intervals, level=self.artifact_correction_level)
        
        # Базовые метрики
        mean_rr = np.mean(rr) if len(rr) > 0 else 0.0
        mean_hr = np.mean(self.hr_data) if len(self.hr_data) > 0 else 0.0
        # SDNN — с откалиброванным детрендингом под Kubios (calibration.json).
        # Fallback на сырой np.std, если калибровка/neurokit недоступны или окно короткое.
        if len(rr) > 1:
            sdnn = compute_sdnn_calibrated(rr) if _CALIB_OK else np.std(rr, ddof=1)
        else:
            sdnn = 0.0
        
        # ИСПРАВЛЕНО: RMSSD с защитой от сглаженных данных
        if len(rr) > 1:
            rmssd_raw = np.sqrt(np.mean(np.diff(rr)**2))
        else:
            rmssd_raw = 0.0
        
        # Если RMSSD подозрительно низкий относительно SDNN,
        # данные вероятно сглажены — используем оценку через SDNN
        # Для нормальных данных RMSSD ≈ 0.7-1.0 × SDNN
        # Если RMSSD < 0.4 × SDNN — данные сглажены (порог снижен для Polar)
        if sdnn > 0 and rmssd_raw < sdnn * 0.4:
            # Консервативная оценка: RMSSD = 0.7 × SDNN
            rmssd = sdnn * 0.7
            logger.warning(
                f"⚠️ Данные сглажены: RMSSD_raw={rmssd_raw:.1f}мс << SDNN={sdnn:.1f}мс (ratio={rmssd_raw/sdnn:.2f}). "
                f"Используем оценку: {rmssd:.1f}мс"
            )
        else:
            rmssd = rmssd_raw
        
        # DEBUG вывод для проверки
        if len(self.hr_data) > 0 and len(rr) > 0:
            logger.debug(
                f"DEBUG HRV: HR range = {np.min(self.hr_data):.0f}-{np.max(self.hr_data):.0f} уд/мин | "
                f"RR range = {np.min(rr):.0f}-{np.max(rr):.0f} мс | "
                f"SDNN = {sdnn:.1f} мс | RMSSD = {rmssd:.1f} мс | "
                f"Ratio RMSSD/SDNN = {rmssd/sdnn:.2f}" if sdnn > 0 else "Ratio = N/A"
            )
        
        # pNN50 / NN50
        diff_rr = np.diff(rr)  # Разности для расчета
        diff_rr_abs = np.abs(diff_rr)
        nn50 = int(np.sum(diff_rr_abs > 50)) if len(diff_rr_abs) > 0 else 0
        pnn50 = (nn50 / len(diff_rr_abs)) * 100 if len(diff_rr_abs) > 0 else 0
        
        # Poincaré (правильные формулы из предыдущей версии)
        if len(diff_rr) > 1:
            diff_rr_var = np.var(diff_rr, ddof=1)
            rr_var = np.var(rr, ddof=1)
            sd1 = np.sqrt(0.5 * diff_rr_var)  # Правильная формула через var
            sd2_sq = 2 * rr_var - 0.5 * diff_rr_var
            sd2 = np.sqrt(max(0, sd2_sq))  # Защита от отрицательных значений
        else:
            sd1 = 0.0
            sd2 = 0.0
        
        # Stress Index (по Баевскому)
        stress_index = self._calculate_stress_index(rr)
        
        # Частотный анализ
        vlf_power, lf_power, hf_power, lf_hf_ratio, lf_nu, hf_nu, total_power = self._calculate_frequency_powers(rr)
        
        # SNS и PNS (доли мощности спектра)
        sns_percent = (lf_power / total_power * 100) if total_power > 0 else 50
        pns_percent = (hf_power / total_power * 100) if total_power > 0 else 50

        # Kubios PNS/SNS Index (z-нормировка по Tarvainen 2014) — тот же расчёт, что в auto_collector.
        if _CALIB_OK and mean_rr > 0:
            pns_index = compute_pns_index(mean_rr, rmssd, sd1)
            sns_index = compute_sns_index(mean_rr, stress_index, sd1, sd2)
        else:
            pns_index = None
            sns_index = None
        
        # Частота дыхания
        respiratory_rate = self._estimate_respiratory_rate(rr)
        
        # Нелинейные метрики
        dfa_alpha1, dfa_alpha2 = self._calculate_dfa(rr)
        sampen = self._calculate_sampen(rr)
        apen = self._calculate_apen(rr)

        # Биологический возраст
        biological_age = self._calculate_biological_age(rmssd, sdnn, stress_index)
        
        # HRV (вариабельность) - ключевой показатель
        hrv = sdnn  # Используем SDNN как основной показатель вариабельности
        
        return {
            'mean_rr': mean_rr,
            'mean_hr': mean_hr,
            'min_hr': float(np.min(self.hr_data)) if len(self.hr_data) > 0 else 0.0,
            'max_hr': float(np.max(self.hr_data)) if len(self.hr_data) > 0 else 0.0,
            'sdnn': sdnn,
            'rmssd': rmssd,
            'nn50': nn50,
            'pnn50': pnn50,
            'sd1': sd1,
            'sd2': sd2,
            'sd1_sd2_ratio': (sd1 / sd2) if sd2 > 0 else 0.0,
            'stress_index': stress_index,
            'vlf_power': vlf_power,
            'lf_power': lf_power,
            'hf_power': hf_power,
            'lf_hf_ratio': lf_hf_ratio,
            'lf_nu': lf_nu,
            'hf_nu': hf_nu,
            'total_power': total_power,
            'sns_percent': sns_percent,
            'pns_percent': pns_percent,
            'pns_index': pns_index,
            'sns_index': sns_index,
            'respiratory_rate': respiratory_rate,
            'dfa_alpha1': dfa_alpha1,
            'dfa_alpha2': dfa_alpha2,
            'sampen': sampen,
            'apen': apen,
            'biological_age': biological_age,
            'hrv': hrv
        }
    
    def _calculate_stress_index(self, rr_intervals: np.ndarray) -> float:
        """
        Расчет Stress Index по Баевскому
        SI = AMo / (2 × Mo × MxDMn)
        Правильная формула из предыдущей версии
        """
        if len(rr_intervals) < 20:
            return 120.0  # Значение по умолчанию
        
        try:
            rr_array = np.array(rr_intervals)
            
            # Мода (AMo) - наиболее часто встречающееся значение
            # Используем гистограмму с 50 бинами
            hist, bin_edges = np.histogram(rr_array, bins=50)
            
            if len(hist) == 0:
                return 120.0
            
            # Амплитуда моды - процент интервалов в модальном классе
            amo = np.max(hist) / len(rr_array) * 100
            
            # Мода - центр бина с максимальной частотой
            mode_idx = np.argmax(hist)
            mo = (bin_edges[mode_idx] + bin_edges[mode_idx + 1]) / 2
            
            # Вариационный размах
            mx_dmn = np.max(rr_array) - np.min(rr_array)
            
            # Формула индекса напряжения: SI = AMo / (2 * Mo * MxDMn)
            # Mo и MxDMn в секундах для правильного расчета
            if mo > 0 and mx_dmn > 0:
                stress_index = amo / (2 * mo / 1000 * mx_dmn / 1000)
            else:
                stress_index = 120.0
            
            return round(stress_index, 1)
            
        except Exception as e:
            logger.warning(f"Ошибка расчета Stress Index: {e}")
            return 120.0
    
    def _calculate_frequency_powers(self, rr_intervals: np.ndarray, fs: float = 4.0) -> Tuple[float, float, float, float, float, float, float]:
        """
        Расчет частотных компонентов (VLF, LF, HF)
        
        Args:
            rr_intervals: RR интервалы в мс
            fs: Частота дискретизации для интерполяции (Гц)
        
        Returns:
            (vlf_power, lf_power, hf_power, lf_hf_ratio, lf_nu, hf_nu, total_power)
        """
        if len(rr_intervals) < 10:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        
        # Интерполяция к равномерной частоте дискретизации
        time_original = np.cumsum(rr_intervals) / 1000  # в секунды
        if time_original[-1] < 1.0:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        
        time_interp = np.arange(0, time_original[-1], 1/fs)
        if len(time_interp) < 10:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        
        rr_interp = np.interp(time_interp, time_original, rr_intervals)
        
        # Удаление тренда
        rr_detrended = rr_interp - np.mean(rr_interp)
        
        # Спектральный анализ (метод Уэлча)
        nperseg = min(256, len(rr_detrended) // 2)
        if nperseg < 8:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        
        freqs, psd = signal.welch(rr_detrended, fs=fs, nperseg=nperseg)
        
        # VLF диапазон: 0.003-0.04 Гц
        vlf_mask = (freqs >= 0.003) & (freqs < 0.04)
        vlf_power = np.trapezoid(psd[vlf_mask], freqs[vlf_mask]) if np.any(vlf_mask) else 0.0

        # LF диапазон: 0.04-0.15 Гц
        lf_mask = (freqs >= 0.04) & (freqs <= 0.15)
        lf_power = np.trapezoid(psd[lf_mask], freqs[lf_mask]) if np.any(lf_mask) else 0.0
        
        # HF диапазон: 0.15-0.4 Гц
        hf_mask = (freqs >= 0.15) & (freqs <= 0.4)
        hf_power = np.trapezoid(psd[hf_mask], freqs[hf_mask]) if np.any(hf_mask) else 0.0
        
        # Отношение
        lf_hf_ratio = lf_power / hf_power if hf_power > 0 else 1.0

        total_power = vlf_power + lf_power + hf_power
        denom = (total_power - vlf_power)
        lf_nu = (lf_power / denom * 100) if denom > 0 else 50.0
        hf_nu = (hf_power / denom * 100) if denom > 0 else 50.0
        
        return vlf_power, lf_power, hf_power, lf_hf_ratio, lf_nu, hf_nu, total_power

    def _clean_rr_artifacts(self, rr_intervals: np.ndarray, level: str = "medium") -> np.ndarray:
        """
        Простейшая коррекция артефактов RR по пороговой схеме Kubios (без изменения длины).
        """
        rr = np.array(rr_intervals, dtype=float)
        if len(rr) < 5:
            return rr

        thresholds = {
            "very_low": 450.0,
            "low": 350.0,
            "medium": 250.0,
            "strong": 150.0,
            "very_strong": 50.0
        }
        base_threshold_ms = thresholds.get(level, 250.0)
        mean_rr = float(np.mean(rr)) if len(rr) > 0 else 1000.0
        # Масштабируем порог относительно 60 bpm (1000 мс)
        threshold_ms = base_threshold_ms * (mean_rr / 1000.0)

        # Локальная медиана (окно 5)
        window = 5
        median_rr = np.copy(rr)
        for i in range(len(rr)):
            start = max(0, i - window // 2)
            end = min(len(rr), i + window // 2 + 1)
            median_rr[i] = np.median(rr[start:end])

        diff = np.abs(rr - median_rr)
        mask_good = diff <= threshold_ms
        # Физиологические границы
        mask_good &= (rr >= 300.0) & (rr <= 2000.0)

        rr_clean = rr.copy()
        if not mask_good.all():
            # Интерполяция выбросов по медианному порогу
            x = np.arange(len(rr))
            x_good = x[mask_good]
            if len(x_good) < 2:
                return np.full_like(rr, float(np.median(rr)))
            rr_clean[~mask_good] = np.interp(x[~mask_good], x_good, rr[mask_good])
            rr_clean = np.clip(rr_clean, 300.0, 2000.0)

        # Доп. пасс Малика: оставшиеся скачки >20% между СОСЕДНИМИ интервалами
        # (пропуски ударов от разрывов BLE) — именно они раздувают RMSSD.
        for _ in range(3):
            d = np.abs(np.diff(rr_clean))
            bad = np.where(d > 0.20 * rr_clean[:-1])[0] + 1
            if len(bad) == 0:
                break
            good = np.setdiff1d(np.arange(len(rr_clean)), bad)
            if len(good) < 2:
                break
            rr_clean[bad] = np.interp(bad, good, rr_clean[good])

        corrected = int(np.sum(np.abs(rr_clean - rr) > 1e-6))
        if corrected:
            logger.info(f"RR artifact correction: {100*corrected/len(rr):.1f}% (level={level} + Malik20%)")
        return rr_clean
    
    def _estimate_respiratory_rate(self, rr_intervals: np.ndarray, fs: float = 4.0) -> float:
        """
        Оценка частоты дыхания через пик HF-диапазона
        """
        if len(rr_intervals) < 10:
            return 15.0
        
        try:
            time_original = np.cumsum(rr_intervals) / 1000
            if time_original[-1] < 1.0:
                return 15.0
            
            time_interp = np.arange(0, time_original[-1], 1/fs)
            if len(time_interp) < 10:
                return 15.0
            
            rr_interp = np.interp(time_interp, time_original, rr_intervals)
            rr_detrended = rr_interp - np.mean(rr_interp)
            
            nperseg = min(256, len(rr_detrended) // 2)
            if nperseg < 8:
                return 15.0
            
            freqs, psd = signal.welch(rr_detrended, fs=fs, nperseg=nperseg)
            
            # Находим пик в HF диапазоне (0.15-0.4 Гц)
            hf_mask = (freqs >= 0.15) & (freqs <= 0.4)
            if np.any(hf_mask):
                hf_freqs = freqs[hf_mask]
                hf_psd = psd[hf_mask]
                peak_idx = np.argmax(hf_psd)
                peak_freq = hf_freqs[peak_idx]
                respiratory_rate = peak_freq * 60  # Гц → вд/мин
            else:
                respiratory_rate = 15.0
        except:
            respiratory_rate = 15.0
        
        return max(8.0, min(25.0, respiratory_rate))  # Ограничиваем разумными значениями
    
    # Популяционная медиана RMSSD по возрасту (Nunan 2010, Shaffer 2017)
    _RMSSD_NORM_BY_AGE = [
        (20, 50.0), (30, 42.0), (40, 35.0), (50, 30.0), (60, 26.0), (70, 22.0), (80, 19.0),
    ]

    def _rmssd_norm_for_age(self, age: int) -> float:
        """Интерполяция популяционной медианы RMSSD для заданного возраста."""
        if age <= self._RMSSD_NORM_BY_AGE[0][0]:
            return self._RMSSD_NORM_BY_AGE[0][1]
        if age >= self._RMSSD_NORM_BY_AGE[-1][0]:
            return self._RMSSD_NORM_BY_AGE[-1][1]
        for i in range(len(self._RMSSD_NORM_BY_AGE) - 1):
            a1, r1 = self._RMSSD_NORM_BY_AGE[i]
            a2, r2 = self._RMSSD_NORM_BY_AGE[i + 1]
            if a1 <= age <= a2:
                t = (age - a1) / (a2 - a1)
                return r1 + (r2 - r1) * t
        return 35.0

    def _calculate_biological_age(self, rmssd: float, sdnn: float, stress_index: float) -> int | None:
        """
        Биологический возраст по HRV — корректный только при заданном реальном возрасте.

        Логика: считаем сколько лет ты «прибавила» или «скинула» относительно
        популяционной медианы RMSSD для своего возраста.
        Без реального возраста (USER_AGE в env) возвращаем None — потому что
        универсальная формула льстит / клевещет одинаково всем.
        """
        import os
        try:
            real_age = int(os.environ.get("USER_AGE", "0"))
        except (TypeError, ValueError):
            real_age = 0

        if real_age < 15 or real_age > 95:
            return None  # честно: без реального возраста не считаем

        norm_rmssd = self._rmssd_norm_for_age(real_age)
        # Каждые 10 мс отклонения RMSSD от нормы для возраста ≈ 5–7 лет био-возраста
        delta_rmssd = rmssd - norm_rmssd
        bio_delta = -delta_rmssd * 0.6  # лучший RMSSD → меньше bio age
        bio_age = real_age + bio_delta

        # Поправки
        if stress_index > 300:
            bio_age += 3
        if sdnn < 30:
            bio_age += 2
        if sdnn > 80:
            bio_age -= 2

        # Ограничение ±25 лет (Kubios на скриншоте 29.05 показал bio=18 при real=40, дельта -22)
        bio_age = max(real_age - 25, min(real_age + 25, bio_age))
        bio_age = max(18, min(90, int(round(bio_age))))

        logger.info(
            f"Bio age: real={real_age}, RMSSD={rmssd:.1f}мс (норма для возраста {norm_rmssd:.0f}), "
            f"SDNN={sdnn:.1f}, SI={stress_index:.1f} → bio {bio_age} лет (Δ {bio_age - real_age:+d})"
        )

        return bio_age

    def _calculate_sampen(self, rr_intervals: np.ndarray, m: int = 2) -> float:
        """Sample Entropy (SampEn)"""
        if len(rr_intervals) < m + 2:
            return 0.0
        rr = np.array(rr_intervals, dtype=float)
        r = 0.2 * np.std(rr) if np.std(rr) > 0 else 0.0
        if r == 0.0:
            return 0.0

        def _count_similar(m_len):
            count = 0
            total = 0
            for i in range(len(rr) - m_len):
                template = rr[i:i+m_len]
                for j in range(i+1, len(rr) - m_len):
                    window = rr[j:j+m_len]
                    if np.max(np.abs(template - window)) <= r:
                        count += 1
                    total += 1
            return count, total

        c_m, total_m = _count_similar(m)
        c_m1, total_m1 = _count_similar(m+1)
        if c_m == 0 or c_m1 == 0:
            return 0.0
        return float(-np.log(c_m1 / c_m))

    def _calculate_apen(self, rr_intervals: np.ndarray, m: int = 2) -> float:
        """Approximate Entropy (ApEn)"""
        if len(rr_intervals) < m + 2:
            return 0.0
        rr = np.array(rr_intervals, dtype=float)
        r = 0.2 * np.std(rr) if np.std(rr) > 0 else 0.0
        if r == 0.0:
            return 0.0

        def _phi(m_len):
            N = len(rr) - m_len + 1
            C = np.zeros(N)
            for i in range(N):
                template = rr[i:i+m_len]
                dist = np.max(np.abs(rr[i:i+m_len] - rr[:N, None][:, :m_len]), axis=1)
                C[i] = np.sum(dist <= r) / N
            return np.sum(np.log(C + 1e-12)) / N

        return float(_phi(m) - _phi(m+1))

    def _calculate_dfa(self, rr_intervals: np.ndarray) -> Tuple[float, float]:
        """Detrended Fluctuation Analysis (DFA α1/α2)"""
        if len(rr_intervals) < 20:
            return 0.0, 0.0
        rr = np.array(rr_intervals, dtype=float)
        rr = rr - np.mean(rr)
        y = np.cumsum(rr)

        def _dfa_alpha(n_values):
            fluctuations = []
            for n in n_values:
                if n < 4 or n >= len(y):
                    continue
                shape = (len(y) // n, n)
                if shape[0] < 2:
                    continue
                y_reshaped = y[:shape[0] * n].reshape(shape)
                t = np.arange(n)
                rms = []
                for segment in y_reshaped:
                    coeffs = np.polyfit(t, segment, 1)
                    trend = np.polyval(coeffs, t)
                    rms.append(np.sqrt(np.mean((segment - trend) ** 2)))
                fluctuations.append((n, np.mean(rms)))
            if len(fluctuations) < 2:
                return 0.0
            ns = np.array([f[0] for f in fluctuations], dtype=float)
            fs = np.array([f[1] for f in fluctuations], dtype=float)
            coeffs = np.polyfit(np.log(ns), np.log(fs + 1e-12), 1)
            return float(coeffs[0])

        alpha1 = _dfa_alpha([4, 5, 6, 8, 10, 12, 14, 16])
        alpha2 = _dfa_alpha([16, 20, 24, 30, 36, 48, 64])
        return alpha1, alpha2
    
    def calculate_axis_scores(self, metrics: Dict, freq_valid: bool = True) -> Dict[str, float]:
        """
        Расчет показателей для 7 осей паутинки
        
        Returns:
            Словарь с показателями: RD, SR, AD, FL, RC, EN, BL
        """
        m = metrics
        
        # RD - Readiness (Готовность)
        hf_power = m['hf_power'] if freq_valid else None
        lf_hf_ratio = m['lf_hf_ratio'] if freq_valid else None

        rd = round(self._calculate_readiness(
            m['rmssd'], m['sdnn'], m['stress_index'],
            hf_power, lf_hf_ratio
        ))
        
        # SR - Stress Resistance (Стрессоустойчивость) с коррекцией по LF/HF
        sr = round(self._stress_index_to_resistance(m['stress_index'], lf_hf_ratio))
        
        # AD - Adaptability (Адаптивность)
        ad = round(self._sdnn_to_adaptability(m['sdnn']))
        
        # FL - Flexibility (Гибкость НС)
        fl = round(self._flexibility_score(m['pnn50'], m['sd1']))
        
        # RC - Recovery (Восстановление)
        rc = round(self._recovery_score(m['rmssd'], hf_power))
        
        # EN - Endurance (Выносливость)
        en = round(self._endurance_score(m['sd2']))
        
        # BL - Balance (Вегетативный баланс)
        bl = self._balance_score(lf_hf_ratio)
        bl = round(bl) if bl is not None else None
        
        return {
            'RD': rd,
            'SR': sr,
            'AD': ad,
            'FL': fl,
            'RC': rc,
            'EN': en,
            'BL': bl
        }
    
    def _calculate_readiness(self, rmssd: float, sdnn: float, stress_index: float,
                            hf_power: float = None, lf_hf_ratio: float = None) -> float:
        """Готовность по шкале справочника: 50–64 норма, 65–79 хороший, 80–100 отличный.

        Калибровано так, что среднестатистический здоровый взрослый в покое
        получает ~55–65%, а не 95–100%. 80%+ требует одновременно отличных значений
        всех ключевых метрик и сбалансированного LF/HF.
        """
        # RMSSD: 19→30%, 25→50%, 35→65%, 50→75%, 80→85% (асимптота к 90%)
        def rmssd_score(r):
            if r >= 80: return 85
            if r >= 50: return 75 + (r - 50) * 10 / 30  # 50→75, 80→85
            if r >= 35: return 65 + (r - 35) * 10 / 15  # 35→65, 50→75
            if r >= 25: return 50 + (r - 25) * 15 / 10  # 25→50, 35→65
            if r >= 19: return 30 + (r - 19) * 20 / 6
            return max(0, r * 30 / 19)

        # SDNN: 30→40%, 50→55%, 100→75%, 150→85%
        def sdnn_score(s):
            if s >= 150: return 85
            if s >= 100: return 75 + (s - 100) * 10 / 50
            if s >= 50: return 55 + (s - 50) * 20 / 50
            if s >= 30: return 40 + (s - 30) * 15 / 20
            return max(0, s * 40 / 30)

        # SI: 50→75 (норма), 30→65 (ваготония), 150→50, 300→25, 500→5
        def si_score(s):
            if s <= 30: return 60 + s / 30 * 5  # 0→60, 30→65
            if s <= 80: return 65 + (80 - s) / 50 * 20  # пик 50→85
            if s <= 150: return 70 - (s - 80) / 70 * 15  # 80→70, 150→55
            if s <= 300: return 55 - (s - 150) / 150 * 30  # 150→55, 300→25
            if s <= 500: return 25 - (s - 300) / 200 * 20
            return max(0, 5 - (s - 500) * 0.01)

        parts = [(rmssd_score(rmssd), 0.30), (sdnn_score(sdnn), 0.25), (si_score(stress_index), 0.20)]

        if hf_power is not None:
            # HF: 100→55, 500→75, 1000→85
            if hf_power >= 1000: hf_norm = 85
            elif hf_power >= 500: hf_norm = 75 + (hf_power - 500) / 500 * 10
            elif hf_power >= 100: hf_norm = 55 + (hf_power - 100) / 400 * 20
            else: hf_norm = max(0, hf_power / 100 * 55)
            parts.append((hf_norm, 0.15))

        if lf_hf_ratio is not None:
            if 0.7 <= lf_hf_ratio <= 1.5:
                balance_norm = 85  # сердцевина нормы
            elif 0.5 <= lf_hf_ratio < 0.7 or 1.5 < lf_hf_ratio <= 2.0:
                balance_norm = 70
            elif lf_hf_ratio < 0.5:
                balance_norm = max(30, lf_hf_ratio / 0.5 * 60)
            elif lf_hf_ratio <= 4.0:
                balance_norm = max(35, 70 - (lf_hf_ratio - 2) * 17)
            else:
                balance_norm = max(0, 35 - (lf_hf_ratio - 4) * 5)
            parts.append((balance_norm, 0.10))

        weight_sum = sum(w for _, w in parts) if parts else 1.0
        readiness = sum(v * w for v, w in parts) / weight_sum
        return round(readiness)
    
    def _stress_index_to_resistance(self, si: float, lf_hf_ratio: float = None) -> float:
        """Стрессоустойчивость: пик 75% при SI=50–80, не выше. Снижается в обе стороны.

        Низкий SI (ваготония) ≠ хорошая стрессоустойчивость — мобилизация ограничена.
        Высокий SI (стресс) — тоже плохо.
        """
        if si < 20:
            sr = 40 + si * 1.5  # 0→40, 20→70
        elif si < 50:
            sr = 70 + (si - 20) * 5 / 30  # 20→70, 50→75
        elif si <= 80:
            sr = 75
        elif si <= 150:
            sr = 75 - (si - 80) * 15 / 70  # 80→75, 150→60
        elif si <= 300:
            sr = 60 - (si - 150) * 30 / 150  # 150→60, 300→30
        elif si <= 500:
            sr = 30 - (si - 300) * 20 / 200
        else:
            sr = max(0, 10 - (si - 500) * 0.02)

        if lf_hf_ratio is not None and lf_hf_ratio > 4.0:
            penalty = min(25, (lf_hf_ratio - 4) * 5)
            sr = max(0, sr - penalty)
        return round(max(0, min(100, sr)))
    
    def _sdnn_to_adaptability(self, sdnn: float) -> float:
        """Адаптивность: 30→40, 50→60, 80→75, 120→85. Не 100% даже при 200."""
        if sdnn >= 200: return 90
        if sdnn >= 120: return 85 + (sdnn - 120) * 5 / 80
        if sdnn >= 80:  return 75 + (sdnn - 80) * 10 / 40
        if sdnn >= 50:  return 60 + (sdnn - 50) * 15 / 30
        if sdnn >= 30:  return 40 + (sdnn - 30) * 20 / 20
        return max(0, sdnn * 40 / 30)

    def _flexibility_score(self, pnn50: float, sd1: float) -> float:
        """Гибкость: pNN50 5→50, 15→70, 30→85. Не 100% при пиковых."""
        def pnn_score(p):
            if p >= 30: return 85
            if p >= 15: return 70 + (p - 15) * 15 / 15
            if p >= 5:  return 50 + (p - 5) * 20 / 10
            return max(0, p * 50 / 5)
        def sd1_score(s):
            if s >= 50: return 85
            if s >= 25: return 65 + (s - 25) * 20 / 25
            if s >= 15: return 45 + (s - 15) * 20 / 10
            return max(0, s * 45 / 15)
        return round(0.6 * pnn_score(pnn50) + 0.4 * sd1_score(sd1))

    def _recovery_score(self, rmssd: float, hf_power: float = None) -> float:
        """Восстановление: 25→50, 50→70, 80→80, 100→85."""
        def rmssd_score(r):
            if r >= 100: return 85
            if r >= 50:  return 70 + (r - 50) * 15 / 50
            if r >= 25:  return 50 + (r - 25) * 20 / 25
            return max(0, r * 50 / 25)
        rmssd_n = rmssd_score(rmssd)
        if hf_power is None or hf_power <= 0:
            return round(rmssd_n)
        def hf_score(h):
            if h >= 1500: return 85
            if h >= 500:  return 70 + (h - 500) * 15 / 1000
            if h >= 100:  return 50 + (h - 100) * 20 / 400
            return max(0, h * 50 / 100)
        return round(0.5 * rmssd_n + 0.5 * hf_score(hf_power))

    def _endurance_score(self, sd2: float) -> float:
        """Выносливость: 30→40, 50→60, 100→78, 150→85."""
        if sd2 >= 200: return 88
        if sd2 >= 150: return 85 + (sd2 - 150) * 3 / 50
        if sd2 >= 100: return 78 + (sd2 - 100) * 7 / 50
        if sd2 >= 50:  return 60 + (sd2 - 50) * 18 / 50
        if sd2 >= 30:  return 40 + (sd2 - 30) * 20 / 20
        return max(0, sd2 * 40 / 30)
    
    def _balance_score(self, lf_hf_ratio: float):
        """Конвертация LF/HF в процент баланса"""
        if lf_hf_ratio is None:
            return None
        if 0.5 <= lf_hf_ratio <= 2.0:
            return 100
        elif lf_hf_ratio < 0.5:
            return max(50, 50 + lf_hf_ratio * 100)
        elif lf_hf_ratio <= 4.0:
            return max(40, 100 - (lf_hf_ratio - 2) * 30)
        else:
            # Исправлено: более плавное снижение для высоких значений
            # 4→40%, 6→30%, 8→20%, 10→10%, 12→0%
            return max(0, 40 - (lf_hf_ratio - 4) * 5)
    
    def calculate_overall_score(self, axis_scores: Dict[str, float]) -> float:
        """
        Расчет общего балла (0-100)
        """
        weights = {
            'RD': 0.20,
            'SR': 0.20,
            'AD': 0.15,
            'FL': 0.10,
            'RC': 0.15,
            'EN': 0.10,
            'BL': 0.10
        }
        valid = {k: v for k, v in axis_scores.items() if v is not None}
        if not valid:
            return 0
        total_weight = sum(weights[k] for k in valid.keys())
        if total_weight <= 0:
            return 0
        score = sum(valid[k] * weights[k] for k in valid.keys()) / total_weight
        return round(score)
    
    def get_state_text(self, overall_score: float) -> str:
        """Определение текстового состояния по общему баллу"""
        if overall_score >= 80:
            return "Отличное"
        elif overall_score >= 60:
            return "Хорошее"
        elif overall_score >= 40:
            return "Напряжение"
        elif overall_score >= 20:
            return "Сниженное"
        else:
            return "Критическое"
