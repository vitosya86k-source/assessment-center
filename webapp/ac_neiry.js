/** Клиентский Neiry-прокси (упрощённо combo_neiry.py) для live в браузере. */
export function clip(x, lo = 0, hi = 1) {
  return Math.max(lo, Math.min(hi, x));
}

export function computeNeiry({
  hr = null,
  arousal = null,
  valence = null,
  tempo = null,
  pausePct = null,
  pitchStd = null,
  loudIqr = null,
  fidget = null,
  headTilt = null,
  faceOk = null,
}) {
  const s = [];
  if (arousal != null) s.push(clip(arousal));
  if (hr != null) s.push(clip((hr - 60) / 50));
  if (valence != null) s.push(clip(-valence));
  const stress = s.length ? Math.round((100 * s.reduce((a, b) => a + b, 0)) / s.length) : null;

  const f = [];
  if (arousal != null) f.push(clip(1 - arousal));
  if (pitchStd != null) f.push(clip(1 - pitchStd / 40));
  if (tempo != null) f.push(clip(1 - tempo / 40));
  if (pausePct != null) f.push(clip((pausePct - 30) / 70));
  if (headTilt != null) f.push(clip(Math.abs(headTilt) / 30));
  const fatigue = f.length ? Math.round((100 * f.reduce((a, b) => a + b, 0)) / f.length) : null;

  const g = [];
  if (fidget != null) g.push(clip(1 - fidget / 0.03));
  if (headTilt != null) g.push(clip(1 - Math.abs(headTilt) / 25));
  if (faceOk != null) g.push(faceOk ? 1 : 0);
  const engagement = g.length ? Math.round((100 * g.reduce((a, b) => a + b, 0)) / g.length) : null;

  let verdict = '';
  if (stress != null && fatigue != null) {
    if (stress >= 65 && fatigue >= 55) verdict = 'высокая нагрузка + утомление';
    else if (stress >= 65) verdict = 'повышенная активация';
    else if (fatigue >= 55) verdict = 'признаки утомления';
    else verdict = 'в пределах рабочего диапазона';
  }
  return { stress, fatigue, engagement, verdict };
}

export function computeResilience(stressHistory) {
  if (!stressHistory || stressHistory.length < 4) return null;
  const mx = Math.max(...stressHistory);
  const last = stressHistory[stressHistory.length - 1];
  const recovery = mx > 0 ? clip(1 - last / mx) : 0.5;
  const mean = stressHistory.reduce((a, b) => a + b, 0) / stressHistory.length;
  const vol = Math.sqrt(
    stressHistory.reduce((s, x) => s + (x - mean) ** 2, 0) / stressHistory.length,
  );
  const stability = clip(1 - vol / 35);
  return Math.round(100 * (0.55 * recovery + 0.45 * stability));
}

export function saveSession(key, payload) {
  try {
    const raw = localStorage.getItem(key) || '[]';
    const arr = JSON.parse(raw);
    arr.unshift(payload);
    localStorage.setItem(key, JSON.stringify(arr.slice(0, 20)));
  } catch (e) {}
}

export function loadSessions(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || '[]');
  } catch (e) {
    return [];
  }
}
