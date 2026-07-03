"""Neiry-блок: композитные индексы состояния из live-каналов combo.

Live-прокси, НЕ медизмерение. Собираются из уже считающихся каналов
(пульс по видео, дыхание, мимика, голос, поза). Аналог 3-блочной выдачи Neiry
(нагрузка/состояние/эмоция), но по камере+звуку, без ЭЭГ и без датчика.

Калибровка порогов предварительная — уточнить на накопленных сессиях.
"""


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def compute_neiry(*, hr=None, resp=None, valence=None, arousal=None,
                  e_anger=None, e_fear=None, emo_stability=None,
                  tempo=None, pause_pct=None, pitch_std=None, loud_iqr=None,
                  speech_ratio=None, fidget=None, head_tilt=None,
                  face_present=None, gaze_on_screen=None):
    """Возвращает {'stress', 'fatigue', 'engagement': 0-100|None, 'verdict': str}.

    Индексы считаются только по доступным каналам (None пропускаются),
    поэтому в audio-only или без лица всё равно даёт частичную оценку.
    """
    # --- Стресс-индекс: активация + физиологическое и мимическое напряжение ---
    s = []
    if arousal is not None:
        s.append(_clip(arousal))                       # эмоциональная активация
    if hr is not None:
        s.append(_clip((hr - 60) / 50.0))              # учащение пульса
    if resp is not None:
        s.append(_clip((resp - 12) / 14.0))            # учащение дыхания
    if valence is not None:
        s.append(_clip(-valence))                      # негативная валентность
    if e_anger is not None or e_fear is not None:
        s.append(_clip((e_anger or 0) + (e_fear or 0)))  # напряжение мимики
    stress = round(100 * sum(s) / len(s)) if s else None

    # --- Утомление: снижение энергии + монотонность подачи + оседание ---
    f = []
    if arousal is not None:
        f.append(_clip(1 - arousal))                   # низкая активация
    if pitch_std is not None:
        f.append(_clip(1 - pitch_std / 40.0))          # монотонная интонация
    if loud_iqr is not None:
        f.append(_clip(1 - loud_iqr / 8.0))            # плоская подача
    if tempo is not None and (speech_ratio or 0) > 0.1:
        f.append(_clip(1 - tempo / 40.0))              # замедление речи (только когда говорит)
    if pause_pct is not None:
        f.append(_clip((pause_pct - 30) / 70.0))       # рост пауз
    if head_tilt is not None:
        f.append(_clip(abs(head_tilt) / 30.0))         # оседание/наклон головы
    fatigue = round(100 * sum(f) / len(f)) if f else None
    # NB: моргания (PERCLOS) усилят утомление — добавятся при FaceLandmarker.

    # --- Вовлечённость/фокус (поведенческий прокси, метим явно) ---
    # По плану Neiry A.3: взгляд на экран + неподвижность + ориентация головы.
    # Взгляд (gaze) появится с FaceLandmarker; пока — неподвижность + голова ровно +
    # присутствие лица в кадре. Только по видео (в audio-only → None).
    g = []
    if gaze_on_screen is not None:
        g.append(_clip(gaze_on_screen))                # доля времени взгляд на экран
    if fidget is not None:
        g.append(_clip(1 - fidget / 0.03))             # неподвижность (низкое ёрзанье)
    if head_tilt is not None:
        g.append(_clip(1 - abs(head_tilt) / 25.0))     # голова ровно, не отвёрнут/не осел
    if face_present is not None:
        g.append(1.0 if face_present else 0.0)         # лицо в кадре (обращён к камере)
    engagement = round(100 * sum(g) / len(g)) if g else None

    return {"stress": stress, "fatigue": fatigue, "engagement": engagement,
            "verdict": _verdict(stress, fatigue)}


def compute_resilience(stress_history):
    """Стрессоустойчивость/саморегуляция (live-прокси) по окну стресс-индекса → 0-100|None.

    НЕ одномоментна: устойчивость = стресс не залипает на пике (восстанавливается) +
    не скачет (низкая волатильность). Меньше 4 точек окна → None (шумный rPPG, нет динамики).
    Выше = устойчивее. Копинг-стратегии как таковые идут из речевого анализа (не live).
    """
    hist = [s for s in (stress_history or []) if s is not None]
    if len(hist) < 4:
        return None
    tail = hist[-max(2, len(hist) // 3):]
    recent = sum(tail) / len(tail)                       # где стресс СЕЙЧАС (последняя треть)
    level = _clip(1 - recent / 100.0)                    # низкий текущий стресс → устойчив
    mean = sum(hist) / len(hist)
    stdev = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5
    vol_pen = _clip(stdev / 40.0)                         # хаотичные скачки штрафуют
    return round(100 * _clip(level * (1 - 0.4 * vol_pen)))


def summary_cards(*, stress=None, fatigue=None, engagement=None,
                  trust=None, dominance=None,
                  pitch_std=None, pause_pct=None, tempo=None, speech_ratio=None,
                  stress_trend=None):
    """Карточки-«выводы» (раздел B плана Neiry) — человекочитаемые итоги наверх панели.

    Синтез поверх сырых метрик: «информации много — хочется итоги». Возвращает список
    {'label': str, 'text': str}; каналы без сигнала пропускаются. 3–4 карточки:
    Состояние сейчас · Динамика · Как считывается (Тодоров) · Речь.

    trust/dominance — оси Тодорова в 0..100 (или 0..1, нормируем). stress_trend —
    знак изменения стресс-индекса за окно (>0 растёт, <0 спадает); None → без карточки.
    """
    def _pct(v):  # мягко нормируем 0..1 → 0..100
        if v is None:
            return None
        return v * 100 if v <= 1.0 else v

    cards = []

    # 1. Состояние сейчас — по совокупности stress/fatigue/engagement
    st, fa, en = stress, fatigue, engagement
    if st is not None or fa is not None:
        if (st or 0) >= 60 and (fa or 0) >= 60:
            txt = "Напряжён и вымотан — состояние истощения"
        elif (st or 0) >= 60:
            txt = "Напряжён, стресс высокий"
        elif (fa or 0) >= 60:
            txt = "Устал, энергия снижена"
        elif (st or 0) <= 30 and (fa or 0) <= 30:
            txt = "Спокоен и собран, в ресурсе"
        else:
            txt = "Ровное рабочее состояние"
        if en is not None:
            txt += ", сфокусирован" if en >= 60 else (", внимание рассеяно" if en < 40 else "")
        cards.append({"label": "Состояние сейчас", "text": txt})

    # 2. Динамика — только если есть тренд стресса за окно
    if stress_trend is not None and abs(stress_trend) >= 5:
        cards.append({"label": "Динамика",
                      "text": "Стресс растёт к концу" if stress_trend > 0
                              else "Успокаивается, стресс спадает"})

    # 3. Как считывается — оси Тодорова (наше преимущество над Neiry)
    tr, do = _pct(trust), _pct(dominance)
    if tr is not None or do is not None:
        if tr is not None and tr >= 60:
            txt = "Считывается располагающе, вызывает доверие"
        elif tr is not None and tr < 40 and (do or 0) >= 60:
            txt = "Держится настороженно и напористо, доминирует"
        elif do is not None and do >= 60:
            txt = "Считывается доминантно, ведёт"
        elif do is not None and do < 40:
            txt = "Мягкая, уступчивая подача"
        else:
            txt = "Нейтральное считывание, без выраженного доминирования"
        cards.append({"label": "Как считывается", "text": txt})

    # 4. Речь — монотонность/паузы/темп (live-прокси; точнее по диктофону)
    r = []
    if pitch_std is not None:
        r.append("монотонно" if pitch_std < 15 else "интонационно живо")
    if pause_pct is not None and pause_pct >= 45:
        r.append("с паузами, обдумывает")
    if tempo is not None and (speech_ratio or 0) > 0.1:
        if tempo >= 45:
            r.append("темп быстрый, напористо")
        elif tempo <= 20:
            r.append("темп медленный")
    if r:
        cards.append({"label": "Речь", "text": ", ".join(r).capitalize()})

    return cards


def _verdict(stress, fatigue):
    if stress is None and fatigue is None:
        return "Недостаточно сигнала для оценки"
    st, fa = stress or 0, fatigue or 0
    if st >= 60 and fa >= 60:
        return "Высокое напряжение на фоне усталости — состояние истощения"
    if st >= 60:
        return "Повышенное напряжение, активация высокая"
    if fa >= 60:
        return "Признаки утомления — энергия снижена, подача плоская"
    if st <= 30 and fa <= 30:
        return "Спокоен и собран — ресурсное состояние"
    return "Умеренное состояние, без выраженных пиков"
