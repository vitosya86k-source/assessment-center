"""
Генератор дашбордов NeuroHRV
Создает 4 PNG изображения: Профиль, SWOT, Динамика, Памятка
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch, Circle, Polygon, Rectangle
from matplotlib.lines import Line2D
import numpy as np
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import pandas as pd
from config import COLORS, DASHBOARD_CONFIG
from hrv_calculator import HRVCalculator


class DashboardGenerator:
    """Генератор дашбордов NeuroHRV"""
    
    def __init__(self, data: pd.DataFrame, metrics: Dict, axis_scores: Dict[str, float],
                 overall_score: float, state_text: str, context_mode: str = "cognitive"):
        """
        Инициализация генератора
        
        Args:
            data: DataFrame с данными (HR, timestamps и т.д.)
            metrics: Словарь с рассчитанными метриками
            axis_scores: Словарь с показателями осей (RD, SR, AD, FL, RC, EN, BL)
            overall_score: Общий балл (0-100)
            state_text: Текстовое описание состояния
        """
        self.data = data
        self.metrics = metrics
        self.axis_scores = axis_scores
        self.overall_score = overall_score
        self.state_text = state_text
        self.context_mode = context_mode
        
        # Настройка matplotlib для русского языка
        plt.rcParams['font.family'] = 'DejaVu Sans'
        plt.rcParams['axes.unicode_minus'] = False
    
    def _get_color_for_score(self, score: float) -> str:
        """Получить цвет для показателя"""
        if score >= 80:
            return COLORS['excellent']
        elif score >= 60:
            return COLORS['good']
        elif score >= 40:
            return COLORS['normal']
        elif score >= 20:
            return COLORS['low']
        else:
            return COLORS['critical']
    
    def _get_color_for_si(self, si: float) -> str:
        """Получить цвет для Stress Index"""
        if si < 150:
            return COLORS['green_zone']
        elif si < 250:
            return COLORS['yellow_zone']
        else:
            return COLORS['red_zone']

    def _get_duration_seconds(self) -> int:
        """Определить длительность записи в секундах"""
        if 'Duration_Seconds' in self.data.columns:
            try:
                dur = float(self.data['Duration_Seconds'].iloc[0])
                if dur > 0:
                    return int(round(dur))
            except Exception:
                pass
        if 'Timestamp_ISO' in self.data.columns:
            timestamps = pd.to_datetime(self.data['Timestamp_ISO'], errors='coerce')
            if timestamps.notna().any():
                start_ts = timestamps.iloc[0]
                end_ts = timestamps.iloc[-1]
                if pd.notna(start_ts) and pd.notna(end_ts):
                    delta = (end_ts - start_ts).total_seconds()
                    if delta > 0:
                        return int(round(delta))
        if len(self.data) > 1:
            return int(round(len(self.data) - 1))
        return 0

    def _format_duration(self) -> str:
        """Форматировать длительность для заголовка"""
        seconds = self._get_duration_seconds()
        if seconds <= 0:
            return "неизвестно"
        minutes = seconds // 60
        sec = seconds % 60
        return f"{minutes}:{sec:02d}"
    
    def generate_profile(self, filepath: str):
        """Генерация PNG 1: Профиль"""
        fig = plt.figure(figsize=(12, 9.5), facecolor=COLORS['background'])
        fig.patch.set_facecolor(COLORS['background'])

        # Заголовок
        period_start = self.data['Timestamp_ISO'].iloc[0] if 'Timestamp_ISO' in self.data.columns else ""
        period_end = self.data['Timestamp_ISO'].iloc[-1] if 'Timestamp_ISO' in self.data.columns else ""
        date_str = datetime.now().strftime("%d.%m.%Y")
        title = "NeuroHRV — Нейроассессмент"
        subtitle = f"{date_str} | Период: {self._format_duration()} | {period_start} — {period_end}"
        fig.text(0.5, 0.97, title, ha='center', va='top', fontsize=24, fontweight='bold',
                 color=COLORS['text_primary'])
        fig.text(0.5, 0.93, subtitle, ha='center', va='top', fontsize=12,
                 color=COLORS['text_secondary'])

        rr_source = self.data['RR_Source'].iloc[0] if 'RR_Source' in self.data.columns else 'unknown'
        if rr_source == 'derived':
            quality_text = "⚠️ RR рассчитаны из HR — частотные метрики приблизительные"
        elif rr_source == 'pmd':
            quality_text = "✅ RR получены напрямую (PMD)"
        elif rr_source == 'device':
            quality_text = "✅ RR получены напрямую"
        else:
            quality_text = ""
        if quality_text:
            fig.text(0.5, 0.91, quality_text, ha='center', va='top',
                     fontsize=9, color=COLORS['text_secondary'])

        # Прогресс-бары (левая колонка)
        ax_bars = fig.add_axes([0.05, 0.30, 0.45, 0.58])
        ax_bars.set_facecolor(COLORS['background'])
        ax_bars.axis('off')

        AXES_ORDER = ['RD', 'SR', 'AD', 'FL', 'RC', 'EN', 'BL']
        AXES_LABELS = {
            'RD': 'RD Готовность',
            'SR': 'SR Стрессоуст.',
            'AD': 'AD Адаптивн.',
            'FL': 'FL Гибкость НС',
            'RC': 'RC Восстановл.',
            'EN': 'EN Выносливость',
            'BL': 'BL Баланс'
        }
        y_positions = np.linspace(0.90, 0.10, 7)
        bar_x = 0.45
        bar_w = 0.40
        bar_h = 0.06

        for i, key in enumerate(AXES_ORDER):
            label = AXES_LABELS[key]
            score = self.axis_scores.get(key, None)
            score_missing = score is None
            score_value = 0 if score_missing else score
            color = COLORS['text_secondary'] if score_missing else self._get_color_for_score(score_value)
            y = y_positions[i]

            ax_bars.text(0.02, y, label, ha='left', va='center',
                         fontsize=10, color=COLORS['text_primary'], transform=ax_bars.transAxes)

            ax_bars.add_patch(Rectangle((bar_x, y - bar_h/2), bar_w, bar_h,
                                        facecolor=COLORS['bar_background'], edgecolor='none',
                                        transform=ax_bars.transAxes))
            ax_bars.add_patch(Rectangle((bar_x, y - bar_h/2), bar_w * (score_value / 100.0), bar_h,
                                        facecolor=color, edgecolor='none',
                                        transform=ax_bars.transAxes))
            score_text = "—" if score_missing else f"{score_value}%"
            ax_bars.text(bar_x + bar_w + 0.03, y, score_text, ha='left', va='center',
                         fontsize=12, fontweight='bold', color=color, transform=ax_bars.transAxes)

        # Паутинка (правая колонка)
        ax_radar = fig.add_axes([0.55, 0.38, 0.42, 0.45])
        ax_radar.set_facecolor(COLORS['background'])
        ax_radar.axis('off')
        self._draw_spider_chart(ax_radar, 0.5, 0.5, 0.40, show_percentages=False)

        # Общий балл
        fig.text(0.27, 0.22, "ОБЩИЙ БАЛЛ", ha='center', va='bottom',
                 fontsize=12, color=COLORS['text_secondary'])
        fig.text(0.27, 0.18, f"{self.overall_score}", ha='center', va='center',
                 fontsize=36, fontweight='bold', color=self._get_color_for_score(self.overall_score))
        fig.text(0.35, 0.185, "/100", ha='left', va='center',
                 fontsize=14, color=COLORS['text_secondary'])

        # Состояние + био‑возраст
        fig.text(0.76, 0.30, f"Состояние: {self.state_text}", ha='center', va='top',
                 fontsize=11, color=COLORS['text_secondary'])
        bio_age = self.metrics.get('biological_age', 0)
        fig.text(0.76, 0.26, f"Био‑возраст: {bio_age} лет", ha='center', va='top',
                 fontsize=10, color=COLORS['text_secondary'])

        # Нижняя панель метрик
        ax_metrics = fig.add_axes([0.05, 0.02, 0.90, 0.16])
        ax_metrics.set_facecolor(COLORS['background'])
        ax_metrics.axis('off')
        self._draw_additional_metrics(ax_metrics, 0.5, 0.0)

        plt.savefig(filepath, facecolor=COLORS['background'], dpi=150,
                    bbox_inches='tight', pad_inches=0.2)
        plt.close()
    
    def _draw_spider_chart(self, ax, x_center: float, y_center: float, radius: float, show_percentages: bool = True):
        """Рисование паутинковой диаграммы"""
        axis_order = ['RD', 'SR', 'AD', 'FL', 'RC', 'EN', 'BL']
        n_axes = len(axis_order)

        # Углы для каждой оси (начиная с верха, по часовой стрелке)
        angles = [np.pi/2 - i * 2*np.pi/n_axes for i in range(n_axes)]
        angles.append(angles[0])  # Замыкание

        # Значения (нормализованные к 0-1)
        values = [(self.axis_scores.get(key, 0) or 0) / 100.0 for key in axis_order]
        values.append(values[0])  # Замыкание

        # Конвертация в координаты
        values_rad = np.array(values) * radius
        x_coords = x_center + values_rad * np.cos(angles)
        y_coords = y_center + values_rad * np.sin(angles)

        # Фон (концентрические круги)
        for r in [0.25, 0.5, 0.75, 1.0]:
            circle = Circle((x_center, y_center), radius * r, fill=False,
                          edgecolor=COLORS['text_secondary'], alpha=0.2, linewidth=0.5,
                          transform=ax.transAxes)
            ax.add_patch(circle)

        # Оси
        for angle in angles[:-1]:
            x_end = x_center + radius * np.cos(angle)
            y_end = y_center + radius * np.sin(angle)
            ax.plot([x_center, x_end], [y_center, y_end], 'k-', alpha=0.2, linewidth=0.5,
                   transform=ax.transAxes)

        # Полигон данных
        polygon = Polygon(list(zip(x_coords, y_coords)), closed=True,
                        facecolor=COLORS['spider_fill'], edgecolor=COLORS['spider_line'],
                        linewidth=2, transform=ax.transAxes)
        ax.add_patch(polygon)

        # Точки на вершинах
        for x, y in zip(x_coords[:-1], y_coords[:-1]):
            circle = Circle((x, y), 0.008, facecolor=COLORS['spider_line'],
                          edgecolor='white', linewidth=1, transform=ax.transAxes)
            ax.add_patch(circle)

        # Подписи осей (только аббревиатуры, без процентов если show_percentages=False)
        for i, (angle, label) in enumerate(zip(angles[:-1], axis_order)):
            # Позиция подписи (снаружи диаграммы)
            label_radius = radius * 1.25
            x_label = x_center + label_radius * np.cos(angle)
            y_label = y_center + label_radius * np.sin(angle)

            if show_percentages:
                score = self.axis_scores.get(label, 0)
                if score is None:
                    text = f"{label}\n—"
                else:
                    text = f"{label}\n{score}%"
                fontsize = 7
            else:
                text = label
                fontsize = 8

            ax.text(x_label, y_label, text, ha='center', va='center',
                   fontsize=8, fontweight='bold', color=COLORS['text_primary'],
                   transform=ax.transAxes, linespacing=0.9)
    
    def _draw_additional_metrics(self, ax, x_center: float, y_bottom: float):
        """Рисование дополнительных показателей в нижней части"""
        # ИСПРАВЛЕНО: HRV заменен на RMSSD и SDNN, добавлены СНС% и ПНС%
        rmssd = self.metrics.get('rmssd', 0)
        sdnn = self.metrics.get('sdnn', 0)
        stress_index = self.metrics.get('stress_index', 0)
        mean_hr = self.metrics.get('mean_hr', 0)
        mean_rr = self.metrics.get('mean_rr', 0)
        lf_hf = self.metrics.get('lf_hf_ratio', 0)
        resp_rate = self.metrics.get('respiratory_rate', 0)
        sd1_sd2 = self.metrics.get('sd1_sd2_ratio', 0)
        vlf_power = self.metrics.get('vlf_power', 0)
        total_power = self.metrics.get('total_power', 0)
        lf_nu = self.metrics.get('lf_nu', 0)
        hf_nu = self.metrics.get('hf_nu', 0)
        
        # Расчет СНС% и ПНС%
        lf_power = self.metrics.get('lf_power', 0)
        hf_power = self.metrics.get('hf_power', 0)
        total_power = lf_power + hf_power
        sns_percent = (lf_power / total_power * 100) if total_power > 0 else 50
        pns_percent = (hf_power / total_power * 100) if total_power > 0 else 50
        
        freq_valid = self.metrics.get('freq_valid', True)
        metrics_data = [
            ('Stress Index', f"{stress_index:.1f}", self._get_color_for_si(stress_index)),
            ('HR', f"{mean_hr:.0f} уд/мин", COLORS['excellent'] if 50 <= mean_hr <= 100 else COLORS['critical']),
            ('Mean RR', f"{mean_rr:.0f} мс", COLORS['excellent'] if 750 <= mean_rr <= 1000 else COLORS['normal']),
            ('RMSSD', f"{rmssd:.1f} мс", COLORS['excellent'] if rmssd >= 30 else COLORS['normal'] if rmssd >= 15 else COLORS['critical']),
            ('SDNN', f"{sdnn:.1f} мс", COLORS['excellent'] if sdnn >= 50 else COLORS['normal'] if sdnn >= 30 else COLORS['critical']),
            ('SD1/SD2', f"{sd1_sd2:.2f}", COLORS['normal']),
            ('Дыхание', f"{resp_rate:.1f} вд/мин", COLORS['excellent'] if 12 <= resp_rate <= 20 else COLORS['normal'])
        ]
        if freq_valid:
            metrics_data += [
                ('LF/HF', f"{lf_hf:.2f}", COLORS['excellent'] if 0.5 <= lf_hf <= 2.0 else COLORS['normal'] if lf_hf <= 4.0 else COLORS['critical']),
                ('LF n.u.', f"{lf_nu:.0f}%", COLORS['normal'] if 40 <= lf_nu <= 70 else COLORS['critical']),
                ('HF n.u.', f"{hf_nu:.0f}%", COLORS['normal'] if 30 <= hf_nu <= 60 else COLORS['critical']),
                ('VLF', f"{vlf_power:.0f} мс²", COLORS['normal']),
                ('Total', f"{total_power:.0f} мс²", COLORS['normal'])
            ]
        
        cols = 6
        rows = int(np.ceil(len(metrics_data) / cols))
        x_positions = np.linspace(0.08, 0.92, cols)
        y_value = [0.65, 0.25]
        y_label = [0.50, 0.10]

        idx = 0
        for r in range(min(rows, 2)):
            for c in range(cols):
                if idx >= len(metrics_data):
                    break
                name, value, color = metrics_data[idx]
                x_pos = x_positions[c]
                ax.text(x_pos, y_value[r], str(value), ha='center', va='bottom',
                        fontsize=11, fontweight='bold', color=color,
                        transform=ax.transAxes)
                ax.text(x_pos, y_label[r], name, ha='center', va='bottom',
                        fontsize=8, color=COLORS['text_secondary'], transform=ax.transAxes)
                idx += 1
    
    def generate_swot(self, filepath: str):
        """Генерация PNG 2: SWOT-анализ"""
        fig = plt.figure(figsize=(14, 8.5), facecolor=COLORS['background'])
        fig.patch.set_facecolor(COLORS['background'])

        # Заголовок
        date_str = datetime.now().strftime("%d.%m.%Y")
        fig.text(0.5, 0.96, "NeuroHRV — АНАЛИЗ", ha='center', va='top',
                 fontsize=20, fontweight='bold', color=COLORS['text_primary'])
        fig.text(0.5, 0.93, f"{date_str} | Период: {self._format_duration()}",
                 ha='center', va='top', fontsize=11, color=COLORS['text_secondary'])

        # Генерация SWOT текстов
        swot_data = self._generate_swot_texts()

        ax_s = fig.add_axes([0.03, 0.50, 0.46, 0.42])
        ax_w = fig.add_axes([0.51, 0.50, 0.46, 0.42])
        ax_o = fig.add_axes([0.03, 0.04, 0.46, 0.42])
        ax_r = fig.add_axes([0.51, 0.04, 0.46, 0.42])

        self._draw_swot_panel(ax_s, "[+] СИЛЬНЫЕ СТОРОНЫ", swot_data['strengths'], COLORS['excellent'])
        self._draw_swot_panel(ax_w, "[-] ОГРАНИЧЕНИЯ", swot_data['weaknesses'], COLORS['low'])
        self._draw_swot_panel(ax_o, "[*] ВОЗМОЖНОСТИ", swot_data['opportunities'], COLORS['good'])
        self._draw_swot_panel(ax_r, "[!] РИСКИ", swot_data['risks'], COLORS['critical'])

        plt.savefig(filepath, facecolor=COLORS['background'], dpi=150,
                    bbox_inches='tight', pad_inches=0.2)
        plt.close()

    def _draw_swot_panel(self, ax, title: str, items: List[str], color: str):
        import textwrap
        ax.set_facecolor(COLORS['background'])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')

        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_linewidth(2.5)
            spine.set_color(color)

        ax.text(0.5, 0.90, title, fontsize=14, fontweight='bold',
                color=color, ha='center', va='top')

        n = max(1, len(items))
        y_start = 0.78
        y_end = 0.08
        y_step = (y_start - y_end) / max(n, 1)

        for i, item in enumerate(items[:4]):
            y = y_start - i * y_step
            wrapped = textwrap.fill(item, width=48)
            ax.text(0.06, y, f"• {wrapped}", fontsize=10,
                    color=COLORS['text_primary'], va='top',
                    linespacing=1.4)
    
    def _draw_swot_box(self, ax, x: float, y: float, width: float, height: float,
                      title: str, items: List[str], border_color: str):
        """Рисование блока SWOT"""
        # Рамка
        box = FancyBboxPatch((x, y), width, height, boxstyle="round,pad=0.01",
                           facecolor=COLORS['bar_background'], edgecolor=border_color,
                           linewidth=2, transform=ax.transAxes, alpha=0.3)
        ax.add_patch(box)
        
        # Заголовок
        ax.text(x + width/2, y + height - 0.02, title, ha='center', va='top',
               fontsize=13, fontweight='bold', color=border_color, transform=ax.transAxes)
        
        # Элементы списка (с переносом строк)
        import textwrap
        y_pos = y + height - 0.06
        line_height = 0.032
        max_lines = int((height - 0.08) / line_height)
        lines_used = 0
        for item in items[:6]:
            wrapped = textwrap.wrap(item, width=48)
            for j, line in enumerate(wrapped):
                if lines_used >= max_lines or y_pos < y + 0.02:
                    break
                prefix = "• " if j == 0 else "  "
                ax.text(x + 0.02, y_pos, f"{prefix}{line}", ha='left', va='top',
                       fontsize=9, color=COLORS['text_primary'], transform=ax.transAxes)
                y_pos -= line_height
                lines_used += 1
            if lines_used >= max_lines or y_pos < y + 0.02:
                break
    
    def _generate_swot_texts(self) -> Dict[str, List[str]]:
        """Генерация текстов SWOT-анализа"""
        strengths = []
        weaknesses = []
        opportunities = []
        risks = []

        # ИСПРАВЛЕНО: Используем тот же порядок что и в основном дашборде
        AXES_ORDER = ['RD', 'SR', 'AD', 'FL', 'RC', 'EN', 'BL']

        catalog = self._load_text_catalog()
        context = self.context_mode

        def _level_for_score(score: float) -> str:
            if score >= 80:
                return "excellent"
            if score >= 60:
                return "good"
            if score >= 40:
                return "medium"
            if score >= 16:
                return "low"
            return "critical"

        def _pick_axis_text(axis: str, score: float) -> str:
            level = _level_for_score(score)
            variants = catalog['axis_texts'].get(axis, {}).get(level, {}).get(context)
            if not variants:
                variants = catalog['axis_texts'].get(axis, {}).get(level, {}).get('universal', [])
            if not variants:
                return f"{axis} {score:.0f}% — недостаточно данных"
            seed = f"{axis}-{level}-{int(score)}-{self._format_duration()}"
            idx = abs(hash(seed)) % len(variants)
            return variants[idx].format(value=int(round(score)))

        # Сильные стороны (>=60%) — топ-3
        strong_axes = [(k, self.axis_scores.get(k)) for k in AXES_ORDER]
        strong_axes = [(k, v) for k, v in strong_axes if v is not None and v >= 60]
        strong_axes = sorted(strong_axes, key=lambda kv: kv[1], reverse=True)[:3]
        for key, score in strong_axes:
            strengths.append(_pick_axis_text(key, score))

        # Ограничения (<40%) — топ-3 самых низких
        weak_axes = [(k, self.axis_scores.get(k)) for k in AXES_ORDER]
        weak_axes = [(k, v) for k, v in weak_axes if v is not None and v < 40]
        weak_axes = sorted(weak_axes, key=lambda kv: kv[1])[:3]
        for key, score in weak_axes:
            text = _pick_axis_text(key, score)
            if score < 15:
                text = f"⚠️ критически {text}"
            weaknesses.append(text)

        # Возможности — оси <60%
        opp_catalog = catalog.get('opportunities', {})
        opp_context = catalog.get('opportunities_context', {})
        low_axes = [(k, self.axis_scores.get(k)) for k in AXES_ORDER]
        low_axes = [(k, v) for k, v in low_axes if v is not None and v < 60]
        low_axes = sorted(low_axes, key=lambda kv: kv[1])[:3]
        for key, score in low_axes:
            if context in ("cognitive", "physical") and key in opp_context:
                opp_text = opp_context[key].get(context)
                if opp_text:
                    opportunities.append(opp_text)
                    continue
            variants = opp_catalog.get(key, [])
            if variants:
                idx = abs(hash(f"{key}-{int(score)}-{self._format_duration()}")) % len(variants)
                opportunities.append(variants[idx])

        # Риски — по каталогу условий
        for rule in catalog.get('risks', []):
            if self._check_risk_rule(rule):
                variants = rule.get('variants', [])
                if variants:
                    idx = abs(hash(f"{rule['key']}-{self._format_duration()}")) % len(variants)
                    risks.append(variants[idx].format(value=rule.get('value_display')))
            if len(risks) >= 3:
                break

        return {
            'strengths': strengths if strengths else ["Нет выраженных сильных сторон"],
            'weaknesses': weaknesses if weaknesses else ["Нет выраженных ограничений"],
            'opportunities': opportunities if opportunities else ["Рекомендуется поддерживать текущий режим"],
            'risks': risks if risks else ["Критических рисков не выявлено"]
        }

    def _load_text_catalog(self) -> Dict:
        if hasattr(self, "_text_catalog_cache"):
            return self._text_catalog_cache
        import re
        from pathlib import Path

        catalog = {
            "axis_texts": {},
            "opportunities": {},
            "opportunities_context": {},
            "risks": []
        }

        path = Path(__file__).with_name("NEUROHRV_TEXT_CATALOG_FULL.md")
        if not path.exists():
            self._text_catalog_cache = catalog
            return catalog

        axis_re = re.compile(r'^##\\s+\\d+\\.\\s+Ось\\s+([A-Z]{2})')
        level_re = re.compile(r'^###\\s+\\d+\\.\\d+\\s+Уровень\\s+`(\\w+)`')
        list_re = re.compile(r'^\\s*\\d+\\.\\s+\"(.+)\"\\s*$')

        axis = None
        level = None
        context = None
        in_opp_table = False
        in_opp_context_table = False
        in_risk_table = False

        def _context_from_line(line: str):
            if "Когнитивный контекст" in line:
                return "cognitive"
            if "Физический контекст" in line:
                return "physical"
            if "Универсальный контекст" in line:
                return "universal"
            return None

        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip()

                m_axis = axis_re.match(line)
                if m_axis:
                    axis = m_axis.group(1)
                    level = None
                    context = None
                    catalog["axis_texts"].setdefault(axis, {})
                    continue

                m_level = level_re.match(line)
                if m_level:
                    level = m_level.group(1)
                    context = None
                    if axis:
                        catalog["axis_texts"][axis].setdefault(level, {})
                    continue

                ctx = _context_from_line(line)
                if ctx:
                    context = ctx
                    if axis and level:
                        catalog["axis_texts"][axis][level].setdefault(context, [])
                    continue

                m_list = list_re.match(line)
                if m_list and axis and level and context:
                    text = m_list.group(1)
                    catalog["axis_texts"][axis][level][context].append(text)
                    continue

                # Таблицы возможностей и рисков
                if "Каталог возможностей по осям" in line:
                    in_opp_table = True
                    in_opp_context_table = False
                    in_risk_table = False
                    continue
                if "Возможности (контекстные варианты)" in line:
                    in_opp_context_table = True
                    in_opp_table = False
                    in_risk_table = False
                    continue
                if line.startswith("| Условие |") and "Риск" in line:
                    in_risk_table = True
                    in_opp_table = False
                    in_opp_context_table = False
                    continue

                if in_opp_table and line.startswith("|"):
                    parts = [p.strip() for p in line.strip("|").split("|")]
                    if len(parts) >= 5 and parts[0] in {"RD","SR","AD","FL","RC","EN","BL"}:
                        axis_code = parts[0]
                        variants = [p.strip('"') for p in parts[2:5]]
                        catalog["opportunities"][axis_code] = variants
                    continue

                if in_opp_context_table and line.startswith("|"):
                    parts = [p.strip() for p in line.strip("|").split("|")]
                    if len(parts) >= 3 and parts[0] in {"RD","SR","AD","FL","RC","EN","BL"}:
                        axis_code = parts[0]
                        catalog["opportunities_context"].setdefault(axis_code, {})
                        catalog["opportunities_context"][axis_code]["cognitive"] = parts[1].strip('"')
                        catalog["opportunities_context"][axis_code]["physical"] = parts[2].strip('"')
                    continue

                if in_risk_table and line.startswith("|"):
                    parts = [p.strip() for p in line.strip("|").split("|")]
                    if len(parts) >= 4:
                        condition = parts[0]
                        variants = [p.strip('"') for p in parts[1:4]]
                        rule = self._parse_risk_condition(condition, variants)
                        if rule:
                            catalog["risks"].append(rule)

        self._text_catalog_cache = catalog
        return catalog

    def _parse_risk_condition(self, condition: str, variants: List[str]) -> Dict:
        import re
        m = re.match(r'^(SI|pNN50|LF/HF|SDNN|RMSSD|HR|RR|Total Power|VLF)\\s*([<>]=?)\\s*([\\d.]+)', condition)
        if not m:
            return {}
        metric, op, value = m.group(1), m.group(2), float(m.group(3))
        return {"metric": metric, "op": op, "threshold": value, "variants": variants, "key": condition}

    def _check_risk_rule(self, rule: Dict) -> bool:
        if not rule:
            return False
        metric = rule["metric"]
        op = rule["op"]
        threshold = rule["threshold"]
        freq_valid = self.metrics.get('freq_valid', True)

        value = None
        if metric == "SI":
            value = self.metrics.get("stress_index", None)
        elif metric == "pNN50":
            value = self.metrics.get("pnn50", None)
        elif metric == "LF/HF":
            if not freq_valid:
                return False
            value = self.metrics.get("lf_hf_ratio", None)
        elif metric == "SDNN":
            value = self.metrics.get("sdnn", None)
        elif metric == "RMSSD":
            value = self.metrics.get("rmssd", None)
        elif metric == "HR":
            value = self.metrics.get("mean_hr", None)
        elif metric == "RR":
            value = self.metrics.get("respiratory_rate", None)
        elif metric == "Total Power":
            if not freq_valid:
                return False
            value = self.metrics.get("total_power", None)
        elif metric == "VLF":
            if not freq_valid:
                return False
            value = self.metrics.get("vlf_power", None)

        if value is None:
            return False

        rule["value_display"] = f"{value:.1f}" if isinstance(value, float) else str(value)

        if op == ">":
            return value > threshold
        if op == ">=":
            return value >= threshold
        if op == "<":
            return value < threshold
        if op == "<=":
            return value <= threshold
        return False
    
    def generate_dynamics(self, filepath: str):
        """Генерация PNG 3: Динамика"""
        fig = plt.figure(figsize=(12, 9), facecolor=COLORS['background'])
        fig.patch.set_facecolor(COLORS['background'])

        gs = fig.add_gridspec(3, 1, height_ratios=[0.5, 1.2, 2.3], hspace=0.25)

        # Заголовок (компактнее)
        ax_title = fig.add_subplot(gs[0, 0])
        ax_title.set_facecolor(COLORS['background'])
        ax_title.axis('off')
        date_str = datetime.now().strftime("%d.%m.%Y")
        ax_title.text(0.5, 0.7, "NeuroHRV — ДИНАМИКА", ha='center', va='center',
                     fontsize=20, fontweight='bold', color=COLORS['text_primary'])
        ax_title.text(0.5, 0.2, f"{date_str} | Период: {self._format_duration()}", ha='center', va='center',
                     fontsize=11, color=COLORS['text_secondary'])
        
        # Лента состояния и распределение
        ax_ribbon = fig.add_subplot(gs[1, 0])
        ax_ribbon.set_facecolor(COLORS['background'])
        ax_ribbon.axis('off')
        self._draw_state_ribbon(ax_ribbon)
        
        # График HR и события
        ax_hr = fig.add_subplot(gs[2, 0])
        ax_hr.set_facecolor(COLORS['background'])
        self._draw_hr_chart(ax_hr)
        
        fig.subplots_adjust(left=0.03, right=0.97, top=0.93, bottom=0.12)
        plt.savefig(filepath, facecolor=COLORS['background'], dpi=150, bbox_inches='tight', pad_inches=0.2)
        plt.close()
    
    def _draw_state_ribbon(self, ax):
        """Рисование ленты состояния"""
        # Используем рассчитанный stress_index из метрик, если нет в данных
        if 'Approximate_Stress_Index' in self.data.columns:
            si_data = self.data['Approximate_Stress_Index'].values
            # Заменяем NaN на рассчитанное значение
            si_value = self.metrics.get('stress_index', 120.0)
            si_data = np.where(np.isnan(si_data), si_value, si_data)
        else:
            # Используем рассчитанный stress_index для всех точек
            si_value = self.metrics.get('stress_index', 120.0)
            si_data = np.full(len(self.data), si_value)
        
        # Распределение по зонам
        green_count = np.sum(si_data < 150)
        yellow_count = np.sum((si_data >= 150) & (si_data < 250))
        red_count = np.sum(si_data >= 250)
        total = len(si_data)
        
        green_pct = (green_count / total * 100) if total > 0 else 0
        yellow_pct = (yellow_count / total * 100) if total > 0 else 0
        red_pct = (red_count / total * 100) if total > 0 else 0
        
        # ИСПРАВЛЕНО: Добавлена цветная лента состояния по времени
        ax.text(0.1, 0.85, "ЛЕНТА СОСТОЯНИЯ", ha='left', va='center',
               fontsize=11, fontweight='bold', color=COLORS['text_primary'])
        
        # Цветная лента (временная шкала)
        ribbon_y = 0.70
        ribbon_height = 0.08
        ribbon_width = 0.8
        n_points = len(si_data)
        
        if n_points > 0:
            segment_width = ribbon_width / n_points
            for i, si in enumerate(si_data):
                if si < 150:
                    color = COLORS['green_zone']
                elif si < 250:
                    color = COLORS['yellow_zone']
                else:
                    color = COLORS['red_zone']
                
                rect = Rectangle((0.1 + i * segment_width, ribbon_y), segment_width, ribbon_height,
                               facecolor=color, edgecolor='none', transform=ax.transAxes)
                ax.add_patch(rect)
            
            # Подписи времени
            if 'Timestamp_ISO' in self.data.columns:
                ts = pd.to_datetime(self.data['Timestamp_ISO'], errors='coerce')
                if ts.notna().any():
                    start_time = ts.min().strftime("%Y-%m-%d %H:%M:%S")
                    end_time = ts.max().strftime("%Y-%m-%d %H:%M:%S")
                else:
                    start_time = self.data['Timestamp_ISO'].iloc[0]
                    end_time = self.data['Timestamp_ISO'].iloc[-1]
                ax.text(0.1, ribbon_y - 0.03, start_time, fontsize=8, color=COLORS['text_secondary'],
                       transform=ax.transAxes)
                ax.text(0.9, ribbon_y - 0.03, end_time, fontsize=8, color=COLORS['text_secondary'],
                       ha='right', transform=ax.transAxes)
        
        # Распределение по зонам (статистика)
        ax.text(0.1, 0.50, "РАСПРЕДЕЛЕНИЕ ПО ЗОНАМ", ha='left', va='center',
               fontsize=11, fontweight='bold', color=COLORS['text_primary'])
        
        y_pos = 0.30
        bar_width = 0.6
        bar_height = 0.12
        
        # Зеленая зона
        ax.add_patch(Rectangle((0.1, y_pos), bar_width * (green_pct/100), bar_height,
                             facecolor=COLORS['green_zone'], transform=ax.transAxes))
        ax.text(0.75, y_pos + bar_height/2, f"[G] Спокойно: {green_pct:.0f}%",
               ha='left', va='center', fontsize=10, color=COLORS['text_primary'],
               transform=ax.transAxes)
        
        # Желтая зона
        y_pos -= 0.15
        ax.add_patch(Rectangle((0.1, y_pos), bar_width * (yellow_pct/100), bar_height,
                             facecolor=COLORS['yellow_zone'], transform=ax.transAxes))
        ax.text(0.75, y_pos + bar_height/2, f"[Y] Напряжение: {yellow_pct:.0f}%",
               ha='left', va='center', fontsize=10, color=COLORS['text_primary'],
               transform=ax.transAxes)
        
        # Красная зона
        y_pos -= 0.15
        ax.add_patch(Rectangle((0.1, y_pos), bar_width * (red_pct/100), bar_height,
                             facecolor=COLORS['red_zone'], transform=ax.transAxes))
        ax.text(0.75, y_pos + bar_height/2, f"[R] Стресс: {red_pct:.0f}%",
               ha='left', va='center', fontsize=10, color=COLORS['text_primary'],
               transform=ax.transAxes)
    
    def _draw_hr_chart(self, ax):
        """Рисование графика HR"""
        if 'Heart_Rate_bpm' not in self.data.columns and 'RR_Interval_ms' not in self.data.columns:
            return

        # Готовим рабочие данные (сортировка по времени при наличии Timestamp_ISO)
        df = self.data.copy()
        timestamps = None
        time_mode = "index"
        if 'Timestamp_ISO' in df.columns:
            ts = pd.to_datetime(df['Timestamp_ISO'], errors='coerce')
            if ts.notna().any():
                df = df.assign(_ts=ts).sort_values('_ts')
                timestamps = df['_ts']
                time_mode = "datetime"

        # Источник HR: если HR нулевой (PMD), считаем из RR
        if 'Heart_Rate_bpm' in df.columns:
            hr_data = df['Heart_Rate_bpm'].astype(float).values
        else:
            hr_data = np.array([], dtype=float)
        if len(hr_data) == 0 or np.nanmax(hr_data) <= 0:
            if 'RR_Interval_ms' in df.columns:
                rr = df['RR_Interval_ms'].astype(float).values
                hr_data = np.where(rr > 0, 60000.0 / rr, np.nan)
            else:
                return
        
        if timestamps is None:
            timestamps = np.arange(len(hr_data))
        
        ax.set_facecolor(COLORS['background'])
        ax.spines['bottom'].set_color(COLORS['text_secondary'])
        ax.spines['top'].set_color(COLORS['text_secondary'])
        ax.spines['left'].set_color(COLORS['text_secondary'])
        ax.spines['right'].set_color(COLORS['text_secondary'])
        ax.tick_params(colors=COLORS['text_secondary'])
        ax.xaxis.label.set_color(COLORS['text_secondary'])
        ax.yaxis.label.set_color(COLORS['text_secondary'])
        
        ax.plot(timestamps, hr_data, color=COLORS['spider_line'], linewidth=2, label='HR')
        ax.fill_between(timestamps, hr_data, alpha=0.3, color=COLORS['spider_line'])
        
        # Разметка всплесков на графике (меньше, прозрачнее)
        events = self._detect_events(hr_data=hr_data, timestamps=timestamps, time_mode=time_mode)
        for event in events[:5]:  # Максимум 5 событий
            idx = event['index']
            if idx < len(hr_data):
                if event['type'] == 'up':
                    color = COLORS['low']  # Оранжевый
                else:
                    color = COLORS['excellent']  # Бирюзовый

                # Закрашиваем узкую область вокруг события
                window = 8
                start_idx = max(0, idx - window)
                end_idx = min(len(hr_data) - 1, idx + window)
                if time_mode == "datetime":
                    start_t = timestamps.iloc[start_idx]
                    end_t = timestamps.iloc[end_idx]
                else:
                    start_t = start_idx
                    end_t = end_idx
                ax.axvspan(start_t, end_t, color=color, alpha=0.2)

                # Маркеры событий
                event_time = timestamps.iloc[idx] if time_mode == "datetime" else idx
                marker = '^' if event['type'] == 'up' else 'v'
                ax.scatter([event_time], [hr_data[idx]], color=color, s=30, marker=marker, zorder=5)
        
        if len(hr_data) > 0:
            if time_mode == "datetime":
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
                ax.set_xlabel('Время (чч:мм:сс)', color=COLORS['text_secondary'])
            else:
                max_time = len(hr_data) / 60  # Конвертируем секунды в минуты
                ax.set_xlim(0, len(hr_data))
                n_ticks = 6
                tick_positions = np.linspace(0, len(hr_data), n_ticks)
                tick_labels = [f"{int(t/60)}:{int(t%60):02d}" for t in tick_positions]
                ax.set_xticks(tick_positions)
                ax.set_xticklabels(tick_labels)
                ax.set_xlabel('Время (мин:сек)', color=COLORS['text_secondary'])

        ax.set_ylabel('HR (уд/мин)', color=COLORS['text_secondary'])
        ax.set_title('График частоты сердечных сокращений', color=COLORS['text_primary'], fontsize=12)
        ax.grid(True, alpha=0.2, color=COLORS['text_secondary'])

        # Легенда (внизу, чтобы не перекрывать график)
        legend_items = [
            Line2D([0], [0], color=COLORS['spider_line'], lw=2, label='HR'),
            mpatches.Patch(color=COLORS['low'], alpha=0.3, label='Всплеск вверх'),
            mpatches.Patch(color=COLORS['excellent'], alpha=0.3, label='Всплеск вниз')
        ]
        ax.legend(handles=legend_items, loc='upper center', bbox_to_anchor=(0.5, -0.12),
                  frameon=False, fontsize=8, ncol=3)
        
        # Отображение событий (компактный формат)
        events = self._detect_events(hr_data=hr_data, timestamps=timestamps, time_mode=time_mode)
        if events:
            ax.text(0.02, 0.98, "СОБЫТИЯ", ha='left', va='top',
                   fontsize=10, fontweight='bold', color=COLORS['text_primary'],
                   transform=ax.transAxes,
                   bbox=dict(facecolor=COLORS['background'], alpha=0.6, edgecolor='none', boxstyle='round,pad=0.3'))
            y_text = 0.90
            for event in events[:4]:  # Максимум 4 события чтобы не наезжало
                icon = '↑' if event['type'] == 'up' else '↓'
                color = COLORS['low'] if event['type'] == 'up' else COLORS['excellent']

                # Извлекаем только время (HH:MM:SS) из timestamp
                ts = str(event['timestamp'])
                time_only = ts[-8:] if len(ts) >= 8 else ts

                # Компактная строка: ↑ 17:03:55 HR 58→94 (+14%)
                text = f"{icon} {time_only} HR {event['hr_before']}→{event['hr_after']} ({event['change_pct']:+d}%)"
                ax.text(0.02, y_text, text, ha='left', va='top',
                       fontsize=8, color=color, transform=ax.transAxes,
                       bbox=dict(facecolor=COLORS['background'], alpha=0.6, edgecolor='none', boxstyle='round,pad=0.2'))

                y_text -= 0.04
        else:
            ax.text(0.02, 0.98, "СОБЫТИЯ", ha='left', va='top',
                   fontsize=10, fontweight='bold', color=COLORS['text_primary'],
                   transform=ax.transAxes,
                   bbox=dict(facecolor=COLORS['background'], alpha=0.6, edgecolor='none', boxstyle='round,pad=0.3'))
            ax.text(0.02, 0.90, "Событий не обнаружено", ha='left', va='top',
                   fontsize=8, color=COLORS['text_secondary'], transform=ax.transAxes,
                   bbox=dict(facecolor=COLORS['background'], alpha=0.6, edgecolor='none', boxstyle='round,pad=0.2'))
    
    def _detect_events(self, hr_data: Optional[np.ndarray] = None, timestamps=None, time_mode: str = "index") -> List[dict]:
        """Обнаружение событий (всплесков вверх и вниз)"""
        events = []
        if hr_data is None:
            if 'Heart_Rate_bpm' not in self.data.columns:
                return events
        hr_data = np.array(hr_data, dtype=float)
        if timestamps is None:
            if 'Timestamp_ISO' in self.data.columns:
                timestamps = self.data['Timestamp_ISO'].values
            else:
                timestamps = np.arange(len(hr_data))
        
        # Используем рассчитанный stress_index если нет в данных
        if 'Approximate_Stress_Index' in self.data.columns:
            si_data = self.data['Approximate_Stress_Index'].values
            si_value = self.metrics.get('stress_index', 120.0)
            si_data = np.where(np.isnan(si_data), si_value, si_data)
        else:
            si_value = self.metrics.get('stress_index', 120.0)
            si_data = np.full(len(hr_data), si_value)
        
        # ИСПРАВЛЕНО: Более чувствительное обнаружение событий
        window = max(10, min(20, len(hr_data) // 8))  # адаптивное окно
        hr_threshold = 0.07  # 7% изменение
        
        for i in range(window, len(hr_data)):
            mean_hr = np.nanmean(hr_data[i-window:i])
            if not np.isfinite(mean_hr) or mean_hr <= 0 or not np.isfinite(hr_data[i]):
                continue
            hr_dev = (hr_data[i] - mean_hr) / mean_hr
            
            # Всплеск ВВЕРХ
            if hr_dev > hr_threshold:
                # Получаем временную метку
                if time_mode == "datetime":
                    timestamp = timestamps.iloc[i]
                else:
                    timestamp = f"{i}с"
                
                events.append({
                    'index': i,
                    'timestamp': timestamp,
                    'type': 'up',
                    'hr_before': round(mean_hr),
                    'hr_after': round(hr_data[i]),
                    'change_pct': round(hr_dev * 100)
                })
            
            # Всплеск ВНИЗ
            elif hr_dev < -hr_threshold:
                if time_mode == "datetime":
                    timestamp = timestamps.iloc[i]
                else:
                    timestamp = f"{i}с"
                
                events.append({
                    'index': i,
                    'timestamp': timestamp,
                    'type': 'down',
                    'hr_before': round(mean_hr),
                    'hr_after': round(hr_data[i]),
                    'change_pct': round(hr_dev * 100)
                })

        # Если событий нет — берём максимальные отклонения
        if not events and len(hr_data) > window:
            devs = []
            for i in range(window, len(hr_data)):
                mean_hr = np.nanmean(hr_data[i-window:i])
                if not np.isfinite(mean_hr) or mean_hr <= 0 or not np.isfinite(hr_data[i]):
                    continue
                hr_dev = (hr_data[i] - mean_hr) / mean_hr
                devs.append((i, hr_dev, mean_hr))
            if devs:
                max_up = max(devs, key=lambda x: x[1])
                max_dn = min(devs, key=lambda x: x[1])
                for idx, hr_dev, mean_hr in [max_up, max_dn]:
                    if abs(hr_dev) < 0.05:
                        continue
                    if time_mode == "datetime":
                        timestamp = timestamps.iloc[idx]
                    else:
                        timestamp = f"{idx}с"
                    events.append({
                        'index': idx,
                        'timestamp': timestamp,
                        'type': 'up' if hr_dev > 0 else 'down',
                        'hr_before': round(mean_hr),
                        'hr_after': round(hr_data[idx]),
                        'change_pct': round(hr_dev * 100)
                    })

        # Объединяем последовательные события
        return self._merge_consecutive_events(events)
    
    def _merge_consecutive_events(self, events: List[dict]) -> List[dict]:
        """Объединение последовательных событий одного типа"""
        if not events:
            return []
        
        merged = []
        current = events[0].copy()
        
        for event in events[1:]:
            if (event['type'] == current['type'] and 
                event['index'] - current['index'] < 30):  # В пределах 30 секунд
                current['index'] = event['index']
                current['timestamp'] = event['timestamp']
                current['hr_after'] = event['hr_after']
            else:
                merged.append(current)
                current = event.copy()
        
        merged.append(current)
        return merged
    
    def generate_reference(self, filepath: str):
        """Генерация PNG 4: Памятка"""
        fig, ax = plt.subplots(figsize=(12, 10), facecolor=COLORS['background'])
        fig.patch.set_facecolor(COLORS['background'])
        ax.set_facecolor(COLORS['background'])
        ax.axis('off')
        
        # Заголовок
        ax.text(0.5, 0.98, "NeuroHRV — ПАМЯТКА", ha='center', va='top',
               fontsize=22, fontweight='bold', color=COLORS['text_primary'],
               transform=ax.transAxes)
        
        # Показатели паутинки
        y_pos = 0.90
        ax.text(0.05, y_pos, "ПОКАЗАТЕЛИ ПАУТИНКИ", ha='left', va='top',
               fontsize=14, fontweight='bold', color=COLORS['excellent'],
               transform=ax.transAxes)
        
        axis_descriptions = {
            'RD': 'Готовность — комплексная готовность к нагрузкам',
            'SR': 'Стрессоустойчивость — способность сохранять эффективность под давлением',
            'AD': 'Адаптивность — способность приспосабливаться к изменениям',
            'FL': 'Гибкость НС — скорость переключения между задачами',
            'RC': 'Восстановление — способность быстро восполнять ресурсы',
            'EN': 'Выносливость — устойчивость к длительным нагрузкам',
            'BL': 'Баланс — симпато-вагальный баланс'
        }
        
        y_pos = 0.85
        for key, desc in axis_descriptions.items():
            ax.text(0.08, y_pos, f"{key} — {desc}", ha='left', va='top',
                   fontsize=10, color=COLORS['text_primary'], transform=ax.transAxes)
            y_pos -= 0.05
        
        # Дополнительные показатели
        y_pos -= 0.05
        ax.text(0.05, y_pos, "ДОПОЛНИТЕЛЬНЫЕ ПОКАЗАТЕЛИ", ha='left', va='top',
               fontsize=14, fontweight='bold', color=COLORS['excellent'],
               transform=ax.transAxes)
        
        y_pos -= 0.05
        additional = [
            'HRV — вариабельность сердечного ритма (ключевой показатель)',
            'SI — Stress Index, индекс напряжения по Баевскому (<150 норма)',
            'HR — частота сердечных сокращений (50-100 уд/мин норма)',
            'Mean RR — средний RR-интервал (базовый уровень активации)',
            'NN50 — количество пар RR с разницей >50 мс',
            'pNN50 — доля соседних RR, различающихся >50 мс',
            'LF/HF — соотношение симпатической и парасимпатической активности',
            'LF/HF n.u. — нормализованные доли LF/HF',
            'VLF — мощность очень низких частот (0.003–0.04 Гц)',
            'Total Power — суммарная мощность (VLF+LF+HF)',
            'SD1/SD2 — соотношение краткосрочной и долгосрочной вариабельности',
            'Дыхание — частота дыхания (12-20 вд/мин норма)',
            'Биологический возраст — функциональный возраст по HRV'
        ]
        
        for item in additional:
            ax.text(0.08, y_pos, f"• {item}", ha='left', va='top',
                   fontsize=10, color=COLORS['text_primary'], transform=ax.transAxes)
            y_pos -= 0.04
        
        # Важное примечание
        y_pos -= 0.05
        ax.text(0.05, y_pos, "! ВАЖНО", ha='left', va='top',
               fontsize=14, fontweight='bold', color=COLORS['critical'],
               transform=ax.transAxes)
        
        y_pos -= 0.05
        rr_source = self.data['RR_Source'].iloc[0] if 'RR_Source' in self.data.columns else 'unknown'
        if rr_source == 'derived':
            warning_text = ("Данные получены из HR (пульса) и являются приближёнными.\n"
                           "RR интервалы рассчитываются как: RR = 60000 / HR\n"
                           "Для точных измерений нужен датчик с RR интервалами.")
        elif rr_source == 'pmd':
            warning_text = ("RR интервалы получены напрямую (PMD/SDK).\n"
                           "Качество метрик — высокое.")
        elif rr_source == 'device':
            warning_text = ("RR интервалы получены напрямую от датчика.\n"
                           "Качество метрик — высокое.")
        else:
            warning_text = ("Источник RR неизвестен.\n"
                           "Частотные метрики могут быть неточными.")
        
        ax.text(0.08, y_pos, warning_text, ha='left', va='top',
               fontsize=10, color=COLORS['text_secondary'], transform=ax.transAxes,
               wrap=True)
        
        fig.subplots_adjust(left=0.03, right=0.97, top=0.95, bottom=0.06)
        plt.savefig(filepath, facecolor=COLORS['background'], dpi=150, bbox_inches='tight', pad_inches=0.2)
        plt.close()
    
    def generate_all(self, output_dir: str):
        """Генерация всех 4 PNG дашбордов"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        self.generate_profile(f"{output_dir}/neurohrv_profile_{timestamp}.png")
        self.generate_swot(f"{output_dir}/neurohrv_swot_{timestamp}.png")
        self.generate_dynamics(f"{output_dir}/neurohrv_dynamics_{timestamp}.png")
        self.generate_reference(f"{output_dir}/neurohrv_reference_{timestamp}.png")
        
        return [
            f"{output_dir}/neurohrv_profile_{timestamp}.png",
            f"{output_dir}/neurohrv_swot_{timestamp}.png",
            f"{output_dir}/neurohrv_dynamics_{timestamp}.png",
            f"{output_dir}/neurohrv_reference_{timestamp}.png"
        ]
