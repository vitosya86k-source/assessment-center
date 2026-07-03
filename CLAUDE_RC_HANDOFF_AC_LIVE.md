# Handoff для Claude Code RC — live-замер АЦ 17.06.2026

Документ для продолжения работы в Remote Control (`run_hrv_remote_control.sh`).  
Контекст: идёт ассессмент, участник **Роман**, упражнение **«чай»** (40 мин думает, потом озвучивает). Платформа **Гранатум** в Chrome.

---

## Цель

Один live-инструмент на время АЦ:

1. **rPPG** по лицу участника (пульс, дыхание, RMSSD-тренд) — только **Роман**, не асессоры.
2. **Голос** live (питч, паузы, темп) + полная запись **m4a** в `~/Dropbox/AC_Audio/`.
3. Лог CSV с метками `video_ok`, паузы в отдельном `_pauses.csv`.
4. Участник в Гранатуме **всегда в верхней плитке**; снизу — асессоры (Виталия, иногда Иван).

Пост-обработка (openSMILE, diarization, coping) — отдельно, см. `analysis/`, отчёт по Алексею `reports/alex_full_report.md`. Сейчас нужен **рабочий live**, не офлайн-пайплайн.

---

## Запуск

```bash
cd "/home/user/Dropbox/С винды целиком/с винды/HRV_Monitor_bot/NEW VERSION"
./start_ac_measurement.sh чай    # метки: чай | письма | сотрудник | клиент
```

Скрипт: `start_ac_measurement.sh` → `venv_new/bin/python rppg_screen.py --exercise <метка>`.

**Важно:** запускать из папки `NEW VERSION`, не из `~`.  
Скрипт был с CRLF — чинили через `sed -i 's/\r$//'`.

Remote Control:

```bash
bash "/home/user/Dropbox/С винды целиком/с винды/HRV_Monitor_bot/run_hrv_remote_control.sh"
# URL: /tmp/claude-rc-hrv.url
```

---

## Что меняли в `rppg_screen.py` (Cursor, эта сессия)

| Изменение | Зачем |
|-----------|--------|
| `AudioFileRecorder` — ffmpeg + PulseAudio monitor → m4a в `AC_Audio` | полный звук сессии |
| `session_paths()` — timestamp + exercise в именах | отдельный файл на упражнение |
| Колонка `voice_voiced`, лог `_pauses.csv` (>1 с) | паузы с таймштампами |
| Захват **всего экрана** (не 55% сверху) | нижние плитки попадали в выбор |
| `find_browser_window_monitor()` — X11 `xwininfo` | захват окна браузера |
| `video_ok` в CSV — не писать фейковый пульс при свёрнутом окне | баг: при minimize HR залипал на 67.9 |
| `auto_participant_zone()` + `pick_participant_face()` | только верхние 50% кадра = участник |
| `--participant-top` по умолчанию | не хватать лицо асессора снизу |
| Клавиши `n/s/b/q` через **терминал** (`termios` cbreak + `select`) | OpenCV `waitKey` не работал — фокус не на окне HRV |
| `zone_forehead_roi`, `zone_is_dark` | ROI по плитке если Haar не видит лицо / камера выкл |

Клавиши (латиница, **в терминале** где крутится скрипт): `n` другое лицо, `s` мышь, `b` baseline покоя, `q` стоп.

---

## Что НЕ работает / открытые баги

### 1. Захватывает асессора вместо Романа (частично починено)

- Было: `detect_faces` → самое **крупное** лицо → часто **Виталия** снизу.
- Сделано: авто-зона верхней половины + `pick_participant_face`.
- **Всё ещё:** на скрине `участник: верхняя плитка (ROI)` + **«жду лицо»** — Haar не находит Романа (смотрит вниз, ракурс). ROI лба по зоне есть (`zone_only`), но превью пустое; пульс **105** при SNR 1.6 — скорее **шум**, не валидный rPPG.

**Нужно:** при `zone is not None` **всегда** мерить через `zone_forehead_roi`, не ждать `last_box`. Не показывать HR если SNR < порога. Возможно MediaPipe face mesh вместо Haar.

### 2. Клавиши N/S в UI не работали

Пользователь жаловался. Перенесли в терминал — **проверить на fish**, что cbreak реально ловит `b` без Enter.

### 3. Свёрнутое окно Гранатум

- **m4a** пишется (~11 мин в `20260617_114001_чай.m4a`) — ок.
- **rPPG** при minimize — Chrome не отдаёт кадры; старый код писал залипший HR. Сейчас `video_ok=0` — но нужно убедиться в проде.
- **Правило для пользователя:** Гранатум не сворачивать; другое окно поверх — можно.

### 4. Голоса не разделяются live

Один микс из наушников (Pulse monitor). Diarization — после АЦ через Deepgram на m4a (как у Алексея в `analysis/extract_speaker_audio.py`).

### 5. Последний m4a обрезан

`20260617_120659_чай.m4a` — **44 байта** (краш/убийство процесса без `q`). Нужен graceful shutdown на SIGTERM.

### 6. `gemini` CLI сломан (Node), `claude` CLI OK — не блокер для live.

---

## Расклад Гранатум (3 плитки)

```
┌─────────────────────┐
│   РОМАН (участник)  │  ← мерить ТОЛЬКО это
├──────────┬──────────┤
│ Виталия  │  Иван    │  ← игнорировать
└──────────┴──────────┘
```

Окно **HRV** справа (560px). Захват — окно Chrome через `xwininfo` или левая часть экрана.

---

## Файлы сегодня (17.06.2026)

| Файл | Комментарий |
|------|-------------|
| `data/20260617_114001_чай.csv` | первая сессия, ~538 строк; часть — свёрнутое окно, HR залипал |
| `AC_Audio/20260617_114001_чай.m4a` | ~658 с, основная запись «чай» |
| `data/20260617_115737_чай.csv` | захватил Виталию |
| `data/20260617_120126_чай.csv` | попытка после фиксов |
| `data/20260617_120659_чай.csv` | верхняя плитка ROI, «жду лицо», HR ~105 шум |
| `AC_Audio/20260617_120659_чай.m4a` | **битый** (44 B) |

Для анализа «чай» брать **`114001`** (m4a полный) + обрезать CSV по времени после переключения на Романа, либо перезаписать упражнение.

---

## Архитектура проекта (обязательно)

- `CLAUDE.md` в корне: **запрет LLM SDK с API-ключом**. LLM только через `claude` / `gemini` CLI (subprocess).
- Live-инструмент: `NEW VERSION/rppg_screen.py`
- Офлайн речь: `NEW VERSION/analysis/` (openSMILE в `/tmp` venv из-за кириллицы в путях Dropbox)
- Эталон офлайн: `reports/alex_full_report.md`, `data/alex_*.json`

---

## Приоритетные задачи для RC

1. **Починить rPPG по верхней плитке без Haar** — всегда `zone_forehead_roi` когда `zone` задана; валидность по SNR; не показывать HR при «жду лицо».
2. **Проверить на живом Романе** — превью лба, пульс 60–80 в покое, `b` baseline в терминале.
3. **SIGTERM** → корректно закрыть ffmpeg m4a.
4. (Позже) openSMILE eGeMAPS в live; DNSMOS; mask-drop по окнам.

---

## Быстрый тест

```bash
cd "NEW VERSION"
venv_new/bin/python -c "from rppg_screen import find_browser_window_monitor, find_pulse_monitor_source; print(find_browser_window_monitor()); print(find_pulse_monitor_source())"
DISPLAY=:0 timeout 5 venv_new/bin/python rppg_screen.py --exercise smoke --headless
```

Окружение: Linux X11, `DISPLAY=:0`, PipeWire, `ffmpeg` OK, `xdotool` **не установлен** (sudo недоступен).

---

*Создано: Cursor Agent, 2026-06-17, для передачи в Claude Code Remote Control.*
