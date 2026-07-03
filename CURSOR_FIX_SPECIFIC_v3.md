# 🔧 КОНКРЕТНЫЕ ИСПРАВЛЕНИЯ dashboard_generator.py

## ПРОБЛЕМА 1: Подписи названий и проценты справа сдвинуты

### Симптом на скриншоте:
- Под SR написано "Готовность" (должно быть "Стрессоустойчивость")
- Справа от FL написано "EN 52%" (должно быть "5%")
- Справа от RC написано "BL" (должно быть "8%")

### Диагностика:
В коде два словаря AXES_NAMES (строки 87 и 341). Возможно используется неправильный.
Также похоже что где-то есть дополнительный код который выводит лишние метки.

### Исправление:

**Шаг 1:** Найти ВСЕ места где определяется AXES_NAMES:
```bash
grep -n "AXES_NAMES" dashboard_generator.py
grep -n "'Готовность'" dashboard_generator.py
```

**Шаг 2:** Убедиться что используется ТОЛЬКО ОДИН словарь. Удалить дубликаты.

**Шаг 3:** В функции generate_profile() ЗАМЕНИТЬ весь блок отрисовки прогресс-баров на:

```python
        # Левая часть: прогресс-бары
        AXES_ORDER = ['RD', 'SR', 'AD', 'FL', 'RC', 'EN', 'BL']
        AXES_NAMES = {
            'RD': 'Готовность',
            'SR': 'Стрессоустойчивость',
            'AD': 'Адаптивность',
            'FL': 'Гибкость НС',
            'RC': 'Восстановление',
            'EN': 'Выносливость',
            'BL': 'Баланс'
        }
        
        y_positions = np.linspace(0.80, 0.20, 7)
        bar_width = 0.30
        bar_height = 0.055
        
        for i in range(7):  # Явный индекс вместо enumerate
            key = AXES_ORDER[i]
            name = AXES_NAMES[key]
            score = self.axis_scores.get(key, 0)
            color = self._get_color_for_score(score)
            y_pos = y_positions[i]
            
            # DEBUG: раскомментировать для проверки
            # print(f"DEBUG bar {i}: key={key}, name={name}, score={score}, y={y_pos:.2f}")
            
            # Аббревиатура (слева)
            ax.text(0.05, y_pos + bar_height/2, key, ha='left', va='center',
                   fontsize=14, fontweight='bold', color=COLORS['text_primary'],
                   transform=ax.transAxes)
            
            # Название (под аббревиатурой)
            ax.text(0.05, y_pos - bar_height/2 - 0.01, name, ha='left', va='top',
                   fontsize=10, color=COLORS['text_secondary'], transform=ax.transAxes)
            
            # Прогресс-бар фон
            bar_rect = Rectangle((0.25, y_pos - bar_height/2), bar_width, bar_height,
                               facecolor=COLORS['bar_background'], edgecolor='none',
                               transform=ax.transAxes)
            ax.add_patch(bar_rect)
            
            # Прогресс-бар заливка
            fill_width = bar_width * (score / 100)
            fill_rect = Rectangle((0.25, y_pos - bar_height/2), fill_width, bar_height,
                                facecolor=color, edgecolor='none',
                                transform=ax.transAxes)
            ax.add_patch(fill_rect)
            
            # Процент (справа от бара) - ТОЛЬКО ОДИН РАЗ!
            ax.text(0.57, y_pos, f"{score}%", ha='left', va='center',
                   fontsize=12, fontweight='bold', color=color, transform=ax.transAxes)
        
        # ВАЖНО: НЕ должно быть никакого другого кода который выводит текст в этой области!
```

---

## ПРОБЛЕМА 2: Проценты на паутинке дублируются

### Симптом на скриншоте:
Около каждой вершины паутинки показано ДВА числа (например, "34%" и "34%")

### Причина:
В функции `_draw_spider_chart` проценты выводятся, И возможно где-то ещё есть вывод.

### Исправление:

ЗАМЕНИТЬ функцию `_draw_spider_chart` на:

```python
    def _draw_spider_chart(self, ax, x_center: float, y_center: float, radius: float):
        """Рисование паутинковой диаграммы"""
        axis_order = ['RD', 'SR', 'AD', 'FL', 'RC', 'EN', 'BL']
        n_axes = len(axis_order)
        
        # Углы (начиная с верха, по часовой стрелке)
        angles = [np.pi/2 - i * 2*np.pi/n_axes for i in range(n_axes)]
        angles.append(angles[0])
        
        # Значения
        values = [self.axis_scores.get(key, 0) / 100.0 for key in axis_order]
        values.append(values[0])
        
        # Координаты
        values_rad = np.array(values) * radius
        x_coords = x_center + values_rad * np.cos(angles)
        y_coords = y_center + values_rad * np.sin(angles)
        
        # Фоновые круги
        for r in [0.25, 0.5, 0.75, 1.0]:
            circle = Circle((x_center, y_center), radius * r, fill=False,
                          edgecolor=COLORS['text_secondary'], alpha=0.2, linewidth=0.5,
                          transform=ax.transAxes)
            ax.add_patch(circle)
        
        # Оси
        for angle in angles[:-1]:
            x_end = x_center + radius * np.cos(angle)
            y_end = y_center + radius * np.sin(angle)
            ax.plot([x_center, x_end], [y_center, y_end], 
                   color=COLORS['text_secondary'], alpha=0.3, linewidth=0.5,
                   transform=ax.transAxes)
        
        # Полигон данных
        polygon = Polygon(list(zip(x_coords, y_coords)), closed=True,
                        facecolor=COLORS['spider_fill'], edgecolor=COLORS['spider_line'],
                        linewidth=2, transform=ax.transAxes)
        ax.add_patch(polygon)
        
        # Точки на вершинах
        for x, y in zip(x_coords[:-1], y_coords[:-1]):
            circle = Circle((x, y), 0.012, facecolor=COLORS['spider_line'],
                          edgecolor='white', linewidth=1, transform=ax.transAxes)
            ax.add_patch(circle)
        
        # Подписи осей И проценты - ТОЛЬКО ОДИН РАЗ для каждой оси!
        for i in range(n_axes):
            angle = angles[i]
            label = axis_order[i]
            score = self.axis_scores.get(label, 0)
            
            # Позиция для подписи (снаружи)
            label_r = radius * 1.35
            x_lbl = x_center + label_r * np.cos(angle)
            y_lbl = y_center + label_r * np.sin(angle)
            
            # Подпись оси
            ax.text(x_lbl, y_lbl, label, ha='center', va='center',
                   fontsize=9, fontweight='bold', color=COLORS['text_primary'],
                   transform=ax.transAxes)
            
            # Процент (чуть ближе к центру, под/над подписью оси)
            pct_r = radius * 1.15
            x_pct = x_center + pct_r * np.cos(angle)
            y_pct = y_center + pct_r * np.sin(angle)
            
            # Смещение процента чтобы не накладывался на подпись
            if angle > np.pi/4 and angle < 3*np.pi/4:  # верхняя часть
                y_pct -= 0.03
            elif angle > -3*np.pi/4 and angle < -np.pi/4:  # нижняя часть
                y_pct += 0.03
            
            ax.text(x_pct, y_pct, f"{score}%", ha='center', va='center',
                   fontsize=8, color=COLORS['spider_line'], transform=ax.transAxes)
```

---

## ПРОБЛЕМА 3: Лента состояния — текст времени наезжает

### Симптом:
Временные метки на ленте накладываются друг на друга.

### Исправление в функции `_draw_state_ribbon`:

ЗАМЕНИТЬ блок вывода временных меток:

```python
            # Подписи времени - ТОЛЬКО начало и конец, не на ленте!
            if 'Timestamp_ISO' in self.data.columns:
                start_time = str(self.data['Timestamp_ISO'].iloc[0])[-8:]  # только время HH:MM:SS
                end_time = str(self.data['Timestamp_ISO'].iloc[-1])[-8:]
                
                # Выводим ПОД лентой, не на ней
                ax.text(0.1, ribbon_y - 0.05, start_time, 
                       fontsize=8, color=COLORS['text_secondary'],
                       ha='left', transform=ax.transAxes)
                ax.text(0.9, ribbon_y - 0.05, end_time, 
                       fontsize=8, color=COLORS['text_secondary'],
                       ha='right', transform=ax.transAxes)
```

---

## ПРОБЛЕМА 4: Биологический возраст 74 года

### Диагностика:
RMSSD = 8.9 мс — это КРИТИЧЕСКИ низкое значение!
При таком RMSSD формула правильно даёт ~74 года.

Проблема в РАСЧЁТЕ RMSSD, а не в формуле биологического возраста.

### Проверка в hrv_calculator.py:

**Шаг 1:** Найти функцию расчёта RMSSD:
```bash
grep -n "def.*rmssd" hrv_calculator.py
grep -n "RMSSD" hrv_calculator.py
```

**Шаг 2:** Добавить отладочный вывод:
```python
def calculate_rmssd(rr_intervals):
    """
    RMSSD = √(Σ(RRᵢ₊₁ - RRᵢ)² / (n-1))
    """
    # DEBUG
    print(f"DEBUG RMSSD: RR count={len(rr_intervals)}")
    print(f"DEBUG RMSSD: RR range={min(rr_intervals):.1f} - {max(rr_intervals):.1f} мс")
    
    # RR должны быть в диапазоне 600-1200 мс (при HR 50-100)
    # Если значения 0.6-1.2, то это СЕКУНДЫ — нужно умножить на 1000!
    
    if max(rr_intervals) < 10:  # Вероятно в секундах
        print("WARNING: RR intervals appear to be in SECONDS, converting to ms")
        rr_intervals = rr_intervals * 1000
    
    differences = np.diff(rr_intervals)
    print(f"DEBUG RMSSD: diff range={min(differences):.1f} - {max(differences):.1f} мс")
    
    rmssd = np.sqrt(np.mean(differences ** 2))
    print(f"DEBUG RMSSD: result={rmssd:.1f} мс")
    
    return rmssd
```

**Шаг 3:** Проверить как рассчитываются RR из HR:
```python
def hr_to_rr(hr_bpm):
    """
    RR (мс) = 60000 / HR (уд/мин)
    
    Пример:
    HR = 60 → RR = 1000 мс
    HR = 100 → RR = 600 мс
    """
    # ВАЖНО: результат в МИЛЛИСЕКУНДАХ!
    rr_ms = 60000.0 / hr_bpm
    return rr_ms
```

### Вероятная причина низкого RMSSD:

При расчёте RR из HR с целочисленным округлением:
```
HR: 60, 61, 60, 61... (меняется на ±1)
RR: 1000, 984, 1000, 984... 
diff: 16, 16, 16...
RMSSD ≈ 16 мс
```

Но если HR почти не меняется (сглажен):
```
HR: 60, 60, 60, 60...
RR: 1000, 1000, 1000...
diff: 0, 0, 0...
RMSSD ≈ 0 мс ← ПРОБЛЕМА!
```

### Решение:

Если данные сглажены и RMSSD получается <10 мс, использовать альтернативную оценку:

```python
def calculate_rmssd_robust(rr_intervals):
    """
    Робастный расчёт RMSSD с защитой от сглаженных данных
    """
    differences = np.diff(rr_intervals)
    rmssd = np.sqrt(np.mean(differences ** 2))
    
    # Если RMSSD слишком низкий, данные вероятно сглажены
    # Используем альтернативную оценку через SDNN
    if rmssd < 10:
        sdnn = np.std(rr_intervals)
        # Приближение: RMSSD ≈ SDNN * 0.8 для коротких записей
        rmssd_estimated = sdnn * 0.8
        print(f"WARNING: RMSSD too low ({rmssd:.1f}), using estimate: {rmssd_estimated:.1f}")
        return max(rmssd, rmssd_estimated)
    
    return rmssd
```

---

## ПРОБЛЕМА 5: Дубликат кода в конце файла

### Симптом:
В конце показанного кода есть дубликат функции `_detect_events` (после `_merge_consecutive_events`).

### Исправление:
УДАЛИТЬ дубликат кода после строки с `return merged`:

```python
    def _merge_consecutive_events(self, events: List[dict]) -> List[dict]:
        """Объединение последовательных событий одного типа"""
        if not events:
            return []
        
        merged = []
        current = events[0].copy()
        
        for event in events[1:]:
            if (event['type'] == current['type'] and 
                event['index'] - current['index'] < 30):
                current['index'] = event['index']
                current['timestamp'] = event['timestamp']
                current['hr_after'] = event['hr_after']
            else:
                merged.append(current)
                current = event.copy()
        
        merged.append(current)
        return merged
    
    # УДАЛИТЬ ВСЁ НИЖЕ ЭТОЙ СТРОКИ до generate_reference()
    # ↓↓↓ ЭТО ДУБЛИКАТ — УДАЛИТЬ ↓↓↓
    #     
    #     window = min(30, len(hr_data) // 4)
    #     if window < 5:
    #         return events
    #     ...
    # ↑↑↑ УДАЛИТЬ ДО ЭТОЙ СТРОКИ ↑↑↑
    
    def generate_reference(self, filepath: str):
        # ... продолжение нормального кода
```

---

## ЧЕКЛИСТ ИСПРАВЛЕНИЙ

### В dashboard_generator.py:

1. [ ] Удалить дубликат AXES_NAMES (оставить только один)
2. [ ] Заменить цикл отрисовки прогресс-баров (убрать сдвиг)
3. [ ] Заменить функцию _draw_spider_chart (убрать дублирование процентов)
4. [ ] Исправить вывод времени на ленте состояния
5. [ ] Удалить дубликат кода в конце файла (после _merge_consecutive_events)

### В hrv_calculator.py:

6. [ ] Добавить проверку единиц измерения RR (мс vs с)
7. [ ] Добавить робастную оценку RMSSD для сглаженных данных
8. [ ] Добавить отладочный вывод для проверки

---

## КАК ПРОВЕРИТЬ ПОСЛЕ ИСПРАВЛЕНИЙ

```python
# Добавить в начало generate_profile():
print("="*50)
print("DEBUG: Проверка соответствия")
for i, key in enumerate(['RD', 'SR', 'AD', 'FL', 'RC', 'EN', 'BL']):
    name = AXES_NAMES[key]
    score = self.axis_scores.get(key, 0)
    print(f"  {i}: {key} = {name} = {score}%")
print("="*50)
```

Вывод должен быть:
```
==================================================
DEBUG: Проверка соответствия
  0: RD = Готовность = 34%
  1: SR = Стрессоустойчивость = 71%
  2: AD = Адаптивность = 37%
  3: FL = Гибкость НС = 5%
  4: RC = Восстановление = 8%
  5: EN = Выносливость = 52%
  6: BL = Баланс = 11%
==================================================
```

И на дашборде должно быть ТО ЖЕ САМОЕ!
