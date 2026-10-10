'use strict';
// Заставка после выбора базы данных: 10 секунд — под синтезированную мелодию нарастает «график активных сессий»
// в цветах классов ожиданий, по нему бежит курсор в такт, затем появляются название и автор проекта.
// Мелодия создаётся в браузере (Web Audio), файлов нет. Звук браузер разрешает только после действия
// пользователя: prime() вызывается в обработчике входа; если звук заблокирован (демо-вход без формы),
// на заставке есть кнопка «Включить звук», и мелодия продолжается с текущего места.

const ashvIntro = (() => {
  const BPM = 120, BEAT = 60 / BPM, BAR = BEAT * 4, LENGTH = BAR * 5;   // 5 тактов = 10 с
  const SHOWN_KEY = 'ashv.intro.shown', SOUND_KEY = 'ashv.intro.sound';
  const AUTHOR = 'Песковский Геннадий Владимирович';
  const EMAIL = 'pgv0304@gmail.com';
  const PHONE = '8 961 412 95 95';
  const LAYERS = [   // классы ожиданий снизу вверх, цвета как на главном графике
    ['CPU', '#00CC00'], ['User I/O', '#004AE7'], ['System I/O', '#0094E7'],
    ['Commit', '#E46800'], ['Application', '#C02800'], ['Concurrency', '#8B1A00'],
  ];
  let ctx = null;

  const store = (area, key, value) => {
    try {
      const s = area === 'session' ? sessionStorage : localStorage;
      if (value === undefined) return s.getItem(key);
      s.setItem(key, value);
    } catch (e) { /* приватный режим */ }
    return null;
  };
  const soundOn = () => store('local', SOUND_KEY) !== 'off';

  function prime() {          // синхронно в обработчике действия пользователя
    if (!soundOn()) return;
    try { ctx = ctx || new (window.AudioContext || window.webkitAudioContext)(); ctx.resume(); } catch (e) { ctx = null; }
  }

  // ---------------------------------------------------------------- мелодия
  const hz = (m) => 440 * 2 ** ((m - 69) / 12);
  const CHORDS = [               // C – Am – F – G – C (финал)
    { root: 36, notes: [60, 64, 67] }, { root: 45, notes: [57, 60, 64] }, { root: 41, notes: [57, 60, 65] },
    { root: 43, notes: [59, 62, 67] }, { root: 36, notes: [60, 64, 67, 72] },
  ];
  const MELODY = [               // [такт, доля, нота, длительность в долях]
    [1, 0, 76, 1], [1, 1, 74, 0.5], [1, 1.5, 72, 0.5], [1, 2, 76, 1], [1, 3, 79, 1],
    [2, 0, 77, 1.5], [2, 1.5, 76, 0.5], [2, 2, 74, 1], [2, 3, 72, 1],
    [3, 0, 74, 1], [3, 1, 79, 1], [3, 2, 77, 0.5], [3, 2.5, 76, 0.5], [3, 3, 74, 1],
    [4, 0, 79, 3.5],
  ];

  function noiseBuffer(ac) {
    const b = ac.createBuffer(1, ac.sampleRate * 0.2, ac.sampleRate), d = b.getChannelData(0);
    let x = 12345;
    for (let i = 0; i < d.length; i++) { x = (x * 1103515245 + 12345) & 0x7fffffff; d[i] = (x / 0x3fffffff - 1) * (1 - i / d.length); }
    return b;
  }

  function buildSong(ac, out, t0, skip) {
    const nodes = [];
    const at = (sec) => t0 + sec - skip;              // время песни → время контекста
    const live = (sec, dur) => sec + dur > skip;       // звучит ли ещё нота
    const note = (type, freq, sec, dur, peak, { attack = 0.02, release = 0.25, cutoff = null } = {}) => {
      if (!live(sec, dur + release)) return;
      const o = ac.createOscillator(), g = ac.createGain();
      o.type = type; o.frequency.value = freq;
      const start = Math.max(at(sec), ac.currentTime), end = at(sec + dur);
      g.gain.setValueAtTime(0, start);
      g.gain.linearRampToValueAtTime(peak, Math.min(start + attack, end));
      g.gain.setValueAtTime(peak, Math.max(end, start + attack));
      g.gain.exponentialRampToValueAtTime(0.0001, Math.max(end, start + attack) + release);
      let tail = g;
      if (cutoff) { const f = ac.createBiquadFilter(); f.type = 'lowpass'; f.frequency.value = cutoff; g.connect(f); tail = f; }
      o.connect(g); tail.connect(out);
      o.start(start); o.stop(Math.max(end, start + attack) + release + 0.05);
      nodes.push(o);
    };
    const noise = noiseBuffer(ac);
    CHORDS.forEach((c, bar) => {
      const s = bar * BAR, last = bar === CHORDS.length - 1;
      c.notes.forEach((n) => note('triangle', hz(n), s, last ? BAR - 0.3 : BAR, 0.05, { attack: 0.35, release: last ? 1.2 : 0.4, cutoff: 1800 }));
      note('sine', hz(c.root), s, BEAT * 1.6, 0.22, { release: 0.3 });
      note('sine', hz(c.root), s + BEAT * 2, BEAT * 1.6, 0.18, { release: 0.3 });
      if (!last) {
        for (let i = 0; i < 8; i++) {                          // арпеджио восьмыми
          const n = c.notes[i % c.notes.length] + (i >= 4 ? 12 : 0);
          note('square', hz(n), s + i * BEAT / 2, BEAT / 2 * 0.8, 0.035, { attack: 0.005, release: 0.12, cutoff: 2600 });
        }
        for (let i = 1; i < 8; i += 2) {                       // хай-хэт на слабые доли
          const sec = s + i * BEAT / 2;
          if (!live(sec, 0.1)) continue;
          const src = ac.createBufferSource(), hp = ac.createBiquadFilter(), g = ac.createGain();
          src.buffer = noise; hp.type = 'highpass'; hp.frequency.value = 7000; g.gain.value = 0.05;
          src.connect(hp); hp.connect(g); g.connect(out);
          src.start(Math.max(at(sec), ac.currentTime)); nodes.push(src);
        }
      }
    });
    MELODY.forEach(([bar, beat, n, len]) =>
      note('sine', hz(n), bar * BAR + beat * BEAT, len * BEAT * 0.92, 0.11, { attack: 0.01, release: 0.35 }));
    return nodes;
  }

  function startMusic(skip) {
    if (!ctx || ctx.state !== 'running') return null;
    const master = ctx.createGain(), comp = ctx.createDynamicsCompressor();
    master.gain.value = 0.75;
    master.connect(comp); comp.connect(ctx.destination);
    const t0 = ctx.currentTime + 0.05;
    return { master, nodes: buildSong(ctx, master, t0, skip) };
  }

  // ---------------------------------------------------------------- картинка
  function series(seed, n) {      // гладкий детерминированный ряд для каждого слоя
    let x = seed;
    const rnd = () => (x = (x * 16807) % 2147483647) / 2147483647;
    const pts = [];
    for (let i = 0; i < n; i++) pts.push(rnd());
    return pts.map((_, i) => (pts[Math.max(0, i - 1)] + pts[i] * 2 + pts[Math.min(n - 1, i + 1)]) / 4);
  }

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }

  function play() {
    if (document.querySelector('.intro')) return Promise.resolve();   // уже на экране
    return new Promise((resolve) => {
      store('session', SHOWN_KEY, '1');
      const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
      const root = el('div', 'intro');
      root.setAttribute('role', 'dialog');
      root.setAttribute('aria-label', `DB ASH Viewer. Автор проекта: ${AUTHOR}`);
      const cv = el('canvas', 'intro-cv');
      const text = el('div', 'intro-text');
      const brand = el('div', 'intro-brand'); brand.innerHTML = '<span class="logo">▦</span> DB ASH Viewer';
      const sub = el('div', 'intro-sub', 'Мониторинг производительности Oracle и PostgreSQL в реальном времени');
      const author = el('div', 'intro-author');
      author.append(el('div', 'intro-label', 'Автор проекта'), el('div', 'intro-name', AUTHOR),
        el('div', 'intro-contacts', `${EMAIL} · ${PHONE}`));
      text.append(brand, sub, author);
      const ctrls = el('div', 'intro-ctrls');
      const soundBtn = el('button', 'intro-btn'); soundBtn.type = 'button';
      const skipBtn = el('button', 'intro-btn', 'Пропустить'); skipBtn.type = 'button';
      ctrls.append(soundBtn, skipBtn);
      const bar = el('div', 'intro-progress'); const fill = el('i'); bar.append(fill);
      root.append(cv, text, ctrls, bar);
      document.body.append(root);

      const started = performance.now();
      const elapsed = () => (performance.now() - started) / 1000;
      let music = soundOn() ? startMusic(0) : null;
      const paintSound = () => {
        const running = ctx && ctx.state === 'running' && soundOn() && music;
        soundBtn.textContent = running ? '🔊 Звук' : '🔇 Включить звук';
      };
      paintSound();
      soundBtn.onclick = (ev) => {
        ev.stopPropagation();
        if (music) {                                            // выключить
          music.master.gain.setTargetAtTime(0, ctx.currentTime, 0.05); music = null;
          store('local', SOUND_KEY, 'off');
        } else {                                                // включить с текущего места
          store('local', SOUND_KEY, 'on');
          prime();
          const go = () => { music = startMusic(elapsed()); paintSound(); };
          if (ctx && ctx.state !== 'running') ctx.resume().then(go, paintSound); else go();
        }
        paintSound();
      };

      let done = false, raf = 0, timer = 0;
      const finish = () => {
        if (done) return; done = true;
        cancelAnimationFrame(raf); clearTimeout(timer);
        document.removeEventListener('keydown', onKey, true);
        if (music && ctx) {
          music.master.gain.setTargetAtTime(0, ctx.currentTime, 0.15);
          const nodes = music.nodes;
          setTimeout(() => nodes.forEach((n) => { try { n.stop(); } catch (e) { /* уже остановлен */ } }), 700);
        }
        root.classList.add('intro-out');
        setTimeout(() => { root.remove(); resolve(); }, reduce ? 0 : 600);
      };
      const onKey = (e) => { if (e.key === 'Escape' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); finish(); } };
      skipBtn.onclick = (e) => { e.stopPropagation(); finish(); };
      document.addEventListener('keydown', onKey, true);
      skipBtn.focus();

      if (reduce) {          // без анимации: статичная карточка 3 секунды
        root.classList.add('intro-static');
        timer = setTimeout(finish, 3000);
        return;
      }

      // ---- canvas
      const g = cv.getContext('2d');
      const N = 160;
      const data = LAYERS.map((_, i) => series(97 + i * 131, N));
      const weights = [1.0, 0.75, 0.25, 0.35, 0.55, 0.2];
      const draw = () => {
        const t = elapsed();
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        const W = cv.clientWidth, H = cv.clientHeight;
        if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, W, H);
        const x0 = W * 0.06, x1 = W * 0.94, yb = H * 0.9, ytop = H * 0.52, cap = 7.4;   // пики ниже блока автора
        const yOf = (v) => yb - (v / cap) * (yb - ytop);
        // сетка и линия CPU
        g.strokeStyle = 'rgba(255,255,255,0.06)'; g.lineWidth = 1;
        for (let k = 1; k <= 7; k++) { g.beginPath(); g.moveTo(x0, yOf(k)); g.lineTo(x1, yOf(k)); g.stroke(); }
        const grow = Math.min(1, t / (BAR * 4));                 // график нарастает 8 секунд
        const upto = Math.max(2, Math.floor(grow * (N - 1)));
        const swell = 0.6 + 0.4 * Math.min(1, t / (BAR * 2));   // высота растёт вместе с музыкой
        const pulse = 1 + 0.06 * Math.exp(-((t % BEAT) / BEAT) * 5);  // лёгкий «удар» на каждую долю
        const base = new Array(upto + 1).fill(0);
        LAYERS.forEach(([, color], li) => {
          const top = base.map((b, i) => b + data[li][i] * weights[li] * 2.2 * swell * pulse);
          g.beginPath();
          for (let i = 0; i <= upto; i++) g.lineTo(x0 + (x1 - x0) * i / (N - 1), yOf(top[i]));
          for (let i = upto; i >= 0; i--) g.lineTo(x0 + (x1 - x0) * i / (N - 1), yOf(base[i]));
          g.closePath(); g.fillStyle = color; g.globalAlpha = 0.9; g.fill(); g.globalAlpha = 1;
          for (let i = 0; i <= upto; i++) base[i] = top[i];
        });
        g.setLineDash([6, 6]); g.strokeStyle = 'rgba(255,90,90,0.75)';
        g.beginPath(); g.moveTo(x0, yOf(5)); g.lineTo(x1, yOf(5)); g.stroke(); g.setLineDash([]);
        // курсор на краю графика
        const cx = x0 + (x1 - x0) * upto / (N - 1);
        g.strokeStyle = 'rgba(88,166,255,0.9)'; g.lineWidth = 2;
        g.beginPath(); g.moveTo(cx, ytop - 10); g.lineTo(cx, yb); g.stroke();
        g.strokeStyle = 'rgba(255,255,255,0.15)'; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x0, yb); g.lineTo(x1, yb); g.stroke();
        fill.style.width = `${Math.min(100, t / LENGTH * 100)}%`;
        // поочерёдное появление текста
        brand.classList.toggle('on', t > 0.6);
        sub.classList.toggle('on', t > 2.0);
        author.classList.toggle('on', t > 4.0);
        if (t >= LENGTH) { finish(); return; }
        raf = requestAnimationFrame(draw);
      };
      raf = requestAnimationFrame(draw);
      timer = setTimeout(finish, LENGTH * 1000 + 500);   // страховка, если вкладка в фоне
    });
  }

  const shownThisSession = () => store('session', SHOWN_KEY) === '1';
  return { prime, play, shownThisSession };
})();
