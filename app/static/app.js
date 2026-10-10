'use strict';

// Wait class colours: Oracle Enterprise Manager / ASH Viewer, and PostgreSQL wait_event_type in the same manner
const WC_COLORS = {
  'CPU': '#00CC00', 'Scheduler': '#CCFFCC', 'User I/O': '#004AE7', 'System I/O': '#0094E7',
  'Concurrency': '#8B1A00', 'Application': '#C02800', 'Commit': '#E46800',
  'Configuration': '#5C440B', 'Administrative': '#717354', 'Network': '#9F9371',
  'Queueing': '#C2B79B', 'Cluster': '#C9C2AF',
  'IO': '#004AE7', 'Lock': '#C02800', 'LWLock': '#8B1A00', 'BufferPin': '#E46800', 'IPC': '#0094E7',
  'Timeout': '#717354', 'Client': '#9F9371', 'Extension': '#C2B79B', 'Activity': '#CCFFCC',
  'Other': '#F06EAA', 'Прочее': '#666',
};
const WC_ORDER = Object.keys(WC_COLORS);
const PALETTE = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452',
  '#9a60b4', '#ea7ccc', '#4aa3a2', '#c4b04c', '#d36b6b', '#7a8bd6'];
const DIM_LABELS = {
  oracle: {
    wait_class: 'Класс', event: 'Событие', sql_id: 'SQL ID', session: 'Сессия', username: 'Пользователь',
    module: 'Модуль', program: 'Программа', machine: 'Машина', sql_opname: 'Операция',
    plan_hash: 'Plan hash', session_type: 'Тип сессии',
  },
  postgres: {
    wait_class: 'Класс', event: 'Событие', sql_id: 'Запрос', session: 'Сессия (pid)', username: 'Пользователь',
    module: 'База данных', program: 'Приложение', machine: 'Клиент', sql_opname: 'Операция',
    plan_hash: 'Plan hash', session_type: 'Тип сессии',
  },
};
// kind of the database on screen: 'oracle' or 'postgres' (from /api/targets)
const curKind = () => ((S.targets || []).find((t) => t.name === S.target) || {}).kind || 'oracle';
const dimLabel = (d) => DIM_LABELS[curKind()][d] || d;
const KIND_LABEL = { oracle: 'Oracle', postgres: 'PostgreSQL' };
// session key "inst:sid,serial" -> PostgreSQL pid
const pidOf = (k) => String(k ?? '').replace(/^\d+:/, '').replace(/,.*$/, '');
const DRILL = { wait_class: 'event', event: 'sql_id', sql_id: 'session' };

const S = {
  target: null, range: 900, live: true, from: null, to: null,
  group: 'wait_class', filters: {}, sel: null, tab: 'event', targets: [],
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pad = (n) => String(n).padStart(2, '0');
const fmtTs = (t, sec = true) => { const d = new Date(t * 1000);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}${sec ? ':' + pad(d.getSeconds()) : ''}`; };
const toLocalInput = (t) => { const d = new Date(t * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };

function colorFor(name, i, group) {
  if (group === 'wait_class' && WC_COLORS[name]) return WC_COLORS[name];
  if (name === 'Прочее') return '#666';
  if (name === 'ON CPU') return WC_COLORS.CPU;
  return PALETTE[i % PALETTE.length];
}

async function api(path, params = {}) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) u.searchParams.append(k, v);
  const r = await fetch(u, { credentials: 'same-origin' });
  if (r.status === 401) { showLogin(false); throw new Error('Требуется вход'); }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function curRange() {
  if (S.live || S.from === null) {
    const to = Math.floor(Date.now() / 1000) + 1;
    return { from: to - S.range, to };
  }
  return { from: S.from, to: S.to };
}
function filterParams(extra = {}) {
  const p = { target: S.target, ...extra };
  for (const [k, v] of Object.entries(S.filters)) p['f_' + k] = v;
  return p;
}

// ------------------------------------------------------------------ chart
const chart = echarts.init($('chart'), 'dark', { renderer: 'canvas' });
let lastActivity = null;

chart.on('click', (p) => {
  if (p.componentType !== 'series' || !p.seriesName || p.seriesName === 'CPU cores') return;
  const s = lastActivity.series.find((x) => x.name === p.seriesName);
  if (!s || s.key === '__other__') return;
  addFilter(S.group, s.key ?? '', DRILL[S.group]);
});
chart.on('brushEnd', (e) => {
  const a = e.areas && e.areas[0];
  if (!a) { S.sel = null; } else {
    const [x0, x1] = a.coordRange;
    S.sel = [Math.floor(x0 / 1000), Math.ceil(x1 / 1000)];
  }
  renderSelInfo(); loadDetails();
});
window.addEventListener('resize', () => chart.resize());

function renderChart(a) {
  lastActivity = a;
  const series = a.series.map((s, i) => ({
    name: s.name, type: 'line', stack: 'aas', symbol: 'none', triggerLineEvent: true,
    sampling: 'lttb', lineStyle: { width: 0 }, step: a.bucket > 1 ? false : 'end',
    areaStyle: { opacity: 0.95 }, emphasis: { focus: 'series' },
    color: colorFor(s.name, i, S.group),
    data: a.times.map((t, j) => [t * 1000, s.data[j]]),
  }));
  if (S.group === 'wait_class') {
    series.sort((x, y) => idx(x.name) - idx(y.name));
  }
  if (a.cpu_count) {
    series.push({ name: 'CPU cores', type: 'line', symbol: 'none', data: [],
      markLine: { silent: true, symbol: 'none', lineStyle: { color: '#ff5555', type: 'dashed' },
        label: { position: 'insideEndTop', formatter: `CPU cores = ${a.cpu_count}`, color: '#ff8888' }, data: [{ yAxis: a.cpu_count }] } });
  }
  chart.setOption({
    backgroundColor: 'transparent', animation: false,
    grid: { left: 50, right: 24, top: 40, bottom: 30 },
    legend: { type: 'scroll', top: 4, textStyle: { color: '#ccc' }, data: series.filter((s) => s.name !== 'CPU cores').map((s) => s.name) },
    tooltip: { trigger: 'axis', axisPointer: { type: 'line' }, order: 'seriesDesc',
      valueFormatter: (v) => (v == null ? '—' : v.toFixed(2)) },
    xAxis: { type: 'time', min: a.from * 1000, max: Math.min(a.to, a.times[a.times.length - 1] + a.bucket) * 1000 },
    yAxis: { type: 'value', name: 'Active Sessions', nameTextStyle: { color: '#888' }, splitLine: { lineStyle: { color: '#232a33' } },
      max: (v) => Math.max(v.max, a.cpu_count ? a.cpu_count * 1.1 : 0, 1) },
    toolbox: { right: 20, top: 0, feature: { brush: { type: ['lineX', 'clear'], title: { lineX: 'Выделить интервал', clear: 'Сбросить' } } } },
    brush: { xAxisIndex: 0, brushLink: 'all', brushMode: 'single', throttleType: 'debounce', throttleDelay: 300,
      brushStyle: { color: 'rgba(88,166,255,0.15)', borderColor: '#58a6ff' } },
    series,
  }, { notMerge: true });
  chart.dispatchAction({ type: 'takeGlobalCursor', key: 'brush', brushOption: { brushType: 'lineX', brushMode: 'single' } });
  if (S.sel) {
    chart.dispatchAction({ type: 'brush', areas: [{ brushType: 'lineX', xAxisIndex: 0, coordRange: [S.sel[0] * 1000, S.sel[1] * 1000] }] });
  }
}
const idx = (n) => { const i = WC_ORDER.indexOf(n); return i < 0 ? 99 : i; };

// ------------------------------------------------------------------ tables
function bar(breakdown, total) {
  const parts = Object.entries(breakdown).sort((a, b) => idx(a[0]) - idx(b[0]));
  return `<div>${parts.map(([wc, c]) => `<span title="${esc(wc)}: ${(100 * c / total).toFixed(1)}%"
    style="width:${(100 * c / total).toFixed(2)}%;background:${WC_COLORS[wc] || '#888'}"></span>`).join('')}</div>`;
}
function pctBarRow(r, maxPct) {
  const w = maxPct ? (100 * r.pct / maxPct) : 0;
  return `<td class="bar"><div style="width:${w.toFixed(1)}%">${bar(r.breakdown, r.samples).slice(5, -6)}</div></td>`;
}
function tableHtml(rows, cols) {
  if (!rows.length) return '<div class="empty">Нет активности в выбранном интервале</div>';
  const maxPct = Math.max(...rows.map((r) => r.pct));
  return `<table><thead><tr><th>Активность</th><th>%</th><th>AAS</th>${cols.map((c) => `<th>${c.h}</th>`).join('')}</tr></thead><tbody>${
    rows.map((r) => `<tr>${pctBarRow(r, maxPct)}<td class="num">${r.pct.toFixed(1)}</td><td class="num">${r.aas.toFixed(2)}</td>${
      cols.map((c) => `<td>${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function detailRange() {
  if (S.sel) return { from: S.sel[0], to: S.sel[1] };
  return curRange();
}

async function loadDetails() {
  if (!S.target) return;
  const r = detailRange();
  const p = filterParams({ from: r.from, to: r.to });
  const [sql, sess] = await Promise.all([
    api('/api/top', { ...p, dim: 'sql_id' }), api('/api/top', { ...p, dim: 'session' }),
  ]);
  $('topsql').innerHTML = tableHtml(sql.rows, [
    { h: dimLabel('sql_id'), f: (x) => `<a class="k mono" data-sql="${esc(x.key)}">${esc(x.key)}</a>` },
    { h: 'Тип', f: (x) => esc(x.op || '') },
    { h: 'Текст', f: (x) => `<div class="txt" title="${esc(x.text)}">${esc(x.text)}</div>` },
  ]);
  $('topsess').innerHTML = tableHtml(sess.rows, [
    curKind() === 'postgres'
      ? { h: 'pid', f: (x) => `<a class="k mono" data-f="session" data-v="${esc(x.key)}" title="${esc(x.key)}">${esc(pidOf(x.key))}</a>` }
      : { h: 'Inst:SID,Serial', f: (x) => `<a class="k mono" data-f="session" data-v="${esc(x.key)}">${esc(x.key)}</a>` },
    { h: 'Пользователь', f: (x) => esc(x.username || '') },
    { h: dimLabel('program'), f: (x) => `<div class="txt" title="${esc(x.program)}">${esc(x.program || '')}</div>` },
    { h: curKind() === 'postgres' ? 'База' : 'Модуль', f: (x) => esc(x.module || '') },
  ]);
  await loadTab();
}

async function loadTab() {
  const r = detailRange();
  const p = filterParams({ from: r.from, to: r.to });
  if (S.tab === 'blocking') {
    const b = await api('/api/blocking', p);
    $('toptab').innerHTML = !b.rows.length ? '<div class="empty">Блокировок не обнаружено</div>' :
      `<table><thead><tr><th>Блокирующая сессия</th><th>Ожиданий (сэмплов)</th><th>AAS</th><th>Ожидающих сессий</th>
       <th>Период</th><th>Кто блокирует</th><th>События ожидающих</th><th>SQL ожидающих</th></tr></thead><tbody>${
        b.rows.map((x) => `<tr><td class="mono">${curKind() === 'postgres' ? 'pid ' + esc(pidOf(x.blocker)) : esc(x.blocker)}</td><td class="num">${x.samples}</td>
        <td class="num">${x.aas.toFixed(2)}</td><td class="num">${x.waiters}</td>
        <td>${fmtTs(x.first_ts)} — ${fmtTs(x.last_ts)}</td>
        <td>${x.blocker_info ? esc([x.blocker_info.username, x.blocker_info.program, x.blocker_info.machine].filter(Boolean).join(' / ')) : '<span class="txt">не активна (idle in transaction?)</span>'}</td>
        <td>${x.events.map((e) => `${esc(e.event)} (${e.c})`).join('<br>')}</td>
        <td>${x.sql_ids.map((e) => `<a class="k mono" data-sql="${esc(e.sql_id)}">${esc(e.sql_id)}</a> (${e.c})`).join('<br>')}</td></tr>`).join('')
      }</tbody></table>`;
    return;
  }
  const t = await api('/api/top', { ...p, dim: S.tab });
  const cols = [{ h: dimLabel(S.tab), f: (x) => `<a class="k" data-f="${S.tab}" data-v="${esc(x.key)}">${esc(x.key)}</a>` }];
  if (S.tab === 'event') cols.push({ h: 'Класс', f: (x) => `<span style="color:${WC_COLORS[x.wclass] || '#ccc'}">■</span> ${esc(x.wclass || '')}` });
  $('toptab').innerHTML = tableHtml(t.rows, cols);
}

document.addEventListener('click', (e) => {
  const a = e.target.closest('a.k');
  if (!a) return;
  if (a.dataset.sql) openSql(a.dataset.sql);
  else if (a.dataset.f) addFilter(a.dataset.f, a.dataset.v);
});

// ------------------------------------------------------------------ filters
function addFilter(dim, val, nextGroup) {
  S.filters[dim] = val;
  if (nextGroup && S.group === dim) S.group = nextGroup;
  else if (S.group === dim) S.group = DRILL[dim] || 'wait_class';
  $('group').value = S.group;
  refresh();
}
function renderCrumbs() {
  const keys = Object.keys(S.filters);
  $('crumbs').innerHTML = keys.length
    ? '<span class="lbl">Фильтры:</span>' + keys.map((k) => `<span class="chip">${dimLabel(k)}: <b>${esc(S.filters[k] || '—')}</b><button data-rm="${k}" title="Убрать">✕</button></span>`).join('') +
      ' <button id="clearf">Сбросить все</button>'
    : '<span class="lbl">Кликните по области графика или строке таблицы для детализации</span>';
}
$('crumbs').addEventListener('click', (e) => {
  if (e.target.dataset.rm) { delete S.filters[e.target.dataset.rm]; refresh(); }
  if (e.target.id === 'clearf') { S.filters = {}; S.group = 'wait_class'; $('group').value = S.group; refresh(); }
});
function renderSelInfo() {
  $('selinfo').innerHTML = S.sel
    ? `Детализация за выделенный интервал: <b>${fmtTs(S.sel[0])} — ${fmtTs(S.sel[1])}</b> &nbsp;<a id="selclear">показать весь интервал</a>`
    : 'Детализация за весь интервал графика. Выделите участок графика мышью, чтобы сузить.';
}
$('selinfo').addEventListener('click', (e) => {
  if (e.target.id === 'selclear') { S.sel = null; chart.dispatchAction({ type: 'brush', areas: [] }); renderSelInfo(); loadDetails(); }
});

// ------------------------------------------------------------------ SQL modal
let mChart = null;
async function openSql(sqlId) {
  const pgsql = curKind() === 'postgres';
  $('m-title').textContent = `${pgsql ? 'Запрос' : 'SQL ID'} ${sqlId}`;
  $('m-body').innerHTML = '<div class="empty">Загрузка…</div>';
  $('modal').classList.remove('hidden');
  $('m-filter').classList.remove('hidden');
  $('m-filter').onclick = () => { closeModal(); addFilter('sql_id', sqlId); };
  const r = curRange();
  const [d, act, ev] = await Promise.all([
    api(`/api/sql/${encodeURIComponent(sqlId)}`, { target: S.target }),
    api('/api/activity', { ...filterParams({ from: r.from, to: r.to }), f_sql_id: sqlId, group: 'wait_class', points: 200 }),
    api('/api/top', { ...filterParams({ from: r.from, to: r.to }), f_sql_id: sqlId, dim: 'event', limit: 10 }),
  ]);
  const kv = (pgsql
    ? [['База данных', d.schema], ['Пользователь', d.module], ['Выполнений', d.executions],
      ['Время всего, с', d.elapsed_s], ['Среднее, мс', d.elapsed_per_exec_ms], ['Строк', d.rows],
      ['Блоков прочитано', d.buffer_gets], ['С диска', d.disk_reads], ['Попадание в кэш, %', d.hit_pct],
      ['Временных блоков', d.temp_blocks], ['Ввод-вывод, с', d.io_s], ['queryid', d.queryid]]
    : [['Схема', d.schema], ['Выполнений', d.executions], ['Elapsed, с', d.elapsed_s],
      ['Elapsed/exec, мс', d.elapsed_per_exec_ms], ['CPU, с', d.cpu_s], ['Buffer gets', d.buffer_gets],
      ['Disk reads', d.disk_reads], ['Строк', d.rows], ['Plan hash', d.plan_hash_value], ['Модуль', d.module],
      ['Последняя активность', d.last_active_time]]).filter(([, v]) => v !== undefined && v !== null);
  $('m-body').innerHTML = `
    ${d.error ? `<div class="status error">${esc(d.error)}</div>` : ''}
    <div class="kv">${kv.map(([k, v]) => `<div><span>${k}</span>${esc(typeof v === 'number' ? v.toLocaleString('ru') : v)}</div>`).join('')}</div>
    <h4>Активность в текущем интервале</h4><div id="m-chart" class="mchart"></div>
    <h4>События</h4>${tableHtml(ev.rows, [{ h: 'Событие', f: (x) => esc(x.key) }])}
    ${d.note ? `<p class="muted small">${esc(d.note)}</p>` : ''}
    <h4>Текст ${pgsql ? 'запроса' : 'SQL'}</h4><pre>${esc(d.text || (pgsql ? 'текст недоступен' : 'текст недоступен (курсор вытеснен из shared pool)'))}</pre>
    ${d.normalized && d.normalized !== d.text ? `<h4>Нормализованный текст (pg_stat_statements)</h4><pre>${esc(d.normalized)}</pre>` : ''}
    ${d.plan ? `<h4>План выполнения (${pgsql ? 'EXPLAIN' : 'DBMS_XPLAN.DISPLAY_CURSOR'})</h4><pre class="plan">${esc(d.plan)}</pre>` : ''}`;
  if (mChart) mChart.dispose();
  mChart = echarts.init($('m-chart'), 'dark');
  mChart.setOption({
    backgroundColor: 'transparent', animation: false, grid: { left: 40, right: 10, top: 10, bottom: 24 },
    tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : v.toFixed(2)) },
    xAxis: { type: 'time' }, yAxis: { type: 'value', splitLine: { lineStyle: { color: '#232a33' } } },
    series: act.series.sort((x, y) => idx(x.name) - idx(y.name)).map((s) => ({
      name: s.name, type: 'line', stack: 'a', symbol: 'none', lineStyle: { width: 0 }, areaStyle: { opacity: 0.95 },
      color: WC_COLORS[s.name] || '#888', data: act.times.map((t, j) => [t * 1000, s.data[j]]) })),
  });
}
function closeModal() { $('modal').classList.add('hidden'); $('modal').classList.remove('over-login'); }
$('m-close').onclick = closeModal;
$('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

// ------------------------------------------------------------------ main loop
async function loadTargets() {
  S.targets = await api('/api/targets');
  const sel = $('target');
  const names = S.targets.map((t) => t.name);
  if ([...sel.options].map((o) => o.value).join('|') !== names.join('|')) {
    sel.innerHTML = names.map((n) => `<option>${esc(n)}</option>`).join('');
    let saved = null;
    try { saved = localStorage.getItem('dbash.target'); } catch (e) { /* storage unavailable */ }
    if (!names.includes(S.target)) S.target = names.includes(saved) ? saved : names[0];
    sel.value = S.target;
  }
  const t = S.targets.find((x) => x.name === S.target);
  if (t) {
    const st = $('status');
    st.className = 'status ' + t.status;
    st.textContent = { running: '● сбор данных', error: '● ошибка', connecting: '● подключение', starting: '● запуск' }[t.status] || t.status;
    st.title = t.error || `${t.type} / ${t.source}, интервал ${t.interval}с` + (t.last_sample ? `, последний сэмпл ${fmtTs(t.last_sample)}` : '');
    const i = t.info || {};
    $('dbinfo').textContent = (t.kind === 'postgres'
      ? [i.db_name, i.host, i.version && `PostgreSQL ${i.version}`, i.cpu_count && `${i.cpu_count} CPU`]
      : [i.db_name, i.instance && `inst ${i.instance}`, i.host, i.version, i.cpu_count && `${i.cpu_count} CPU`]).filter(Boolean).join(' · ');
    applyKindLabels(t.kind);
    if (t.error) $('dbinfo').textContent = t.error;
    const lt = $('loadTag'), l = t.load;
    lt.classList.toggle('hidden', !l);
    if (l) {
      const phase = l.status === 'error' ? 'ошибка' : l.phase === 'pause' ? 'пауза' : (l.phase || l.status);
      lt.textContent = `Нагрузка: ${phase}`;
      lt.className = 'status ' + (l.status === 'error' ? 'error' : 'running');
      lt.title = l.status === 'error' ? `Генератор нагрузки: ${l.error}` :
        `Нагрузочное тестирование${l.user ? ` под ${l.user}` : ''}, до ${l.sessions} сессий; текущая фаза: ${l.phase || '—'}`;
    }
  }
}

let shownKind = null;
function applyKindLabels(kind) {
  if (kind === shownKind) return;
  shownKind = kind;
  for (const o of $('group').options) o.textContent = DIM_LABELS[kind][o.value] || o.textContent;
  const tabNames = kind === 'postgres'
    ? { module: 'Базы данных', program: 'Приложения', machine: 'Клиенты' }
    : { module: 'Модули', program: 'Программы', machine: 'Машины' };
  for (const b of $('tabs').children) if (tabNames[b.dataset.d]) b.textContent = tabNames[b.dataset.d];
  const ph = $('tabs').querySelector('[data-d="plan_hash"]');
  if (ph) ph.classList.toggle('hidden', kind === 'postgres');
  if (kind === 'postgres' && S.tab === 'plan_hash') S.tab = 'event';
}

let busy = false;
async function refresh() {
  if (busy || !S.target) return;
  busy = true;
  try {
    renderCrumbs();
    const r = curRange();
    const a = await api('/api/activity', filterParams({ from: r.from, to: r.to, group: S.group }));
    renderChart(a);
    renderSelInfo();
    await loadDetails();
  } catch (e) {
    console.error(e);
    $('selinfo').textContent = 'Ошибка: ' + e.message;
  } finally { busy = false; }
}

$('target').onchange = (e) => { S.target = e.target.value; try { localStorage.setItem('dbash.target', S.target); } catch (err) { /* ignore */ }
  S.filters = {}; S.sel = null; loadTargets(); refresh(); };
$('group').onchange = (e) => { S.group = e.target.value; refresh(); };
$('ranges').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  [...$('ranges').children].forEach((x) => x.classList.toggle('on', x === b));
  if (b.dataset.r === 'custom') {
    $('custom').classList.remove('hidden');
    const r = curRange(); $('cfrom').value = toLocalInput(r.from); $('cto').value = toLocalInput(r.to);
    return;
  }
  $('custom').classList.add('hidden');
  S.range = +b.dataset.r; S.from = null; S.sel = null;
  S.live = true; $('live').classList.add('on');
  refresh();
});
$('capply').onclick = () => {
  const f = Date.parse($('cfrom').value) / 1000, t = Date.parse($('cto').value) / 1000;
  if (!(t > f)) return alert('Неверный интервал');
  S.from = Math.floor(f); S.to = Math.ceil(t); S.live = false; S.sel = null;
  $('live').classList.remove('on'); refresh();
};
$('live').onclick = () => {
  S.live = !S.live; $('live').classList.toggle('on', S.live);
  if (!S.live) { const r = curRange(); S.from = r.from; S.to = r.to; } else { S.from = null; }
  refresh();
};
$('tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  [...$('tabs').children].forEach((x) => x.classList.toggle('on', x === b));
  S.tab = b.dataset.d; loadTab();
});

// ------------------------------------------------------------------ databases and login
// The entry screen lists databases: the server's demo database first, then the connections saved in
// this browser (localStorage; with the password when «Запомнить пароль» is on, so a click reconnects).
let started = false;
const CONN_KEY = 'dbash.connections';
let DEFAULTS = null;
let editingId = null;          // id of the saved connection open in the form, null for a new one
let pendingDelete = null;      // id waiting for the second click on «Удалить»

// Desktop version (DEFAULTS.local_store): the list is kept in a file by the app itself, since the window's
// storage is tied to the address and may be lost; it is loaded once in getDefaults() and written on every change.
let connCache = null;
const conns = {
  all() {
    if (connCache) return connCache.slice();
    try { return JSON.parse(localStorage.getItem(CONN_KEY)) || []; } catch (e) { return []; }
  },
  save(list) {
    try { localStorage.setItem(CONN_KEY, JSON.stringify(list)); } catch (e) { /* storage unavailable */ }
    if (connCache) {
      connCache = list.slice();
      fetch('/api/local-connections', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(list) }).catch(() => toast('Не удалось сохранить список баз данных'));
    }
  },
  get(id) { return this.all().find((c) => c.id === id); },
  put(c) { const l = this.all().filter((x) => x.id !== c.id); l.push(c); this.save(l); },
  remove(id) { this.save(this.all().filter((x) => x.id !== id)); },
};
const connKind = (c) => (c && c.type === 'postgres' ? 'postgres' : 'oracle');
const connLabel = (c) => `${KIND_LABEL[connKind(c)]} · ${c.user}@${c.host}:${c.port}/${c.service}`;

async function getDefaults() {
  if (!DEFAULTS) { try { DEFAULTS = await (await fetch('/api/defaults')).json(); } catch (e) { DEFAULTS = {}; } }
  if (DEFAULTS.local_store && !connCache) {
    try {
      const r = await fetch('/api/local-connections');
      if (r.ok) {
        const fromFile = await r.json();
        const inBrowser = conns.all();
        connCache = fromFile;
        // first start after the update: move what this window had saved into the file
        const known = new Set(fromFile.map((c) => c.id));
        const extra = inBrowser.filter((c) => c && c.id && !known.has(c.id));
        if (extra.length) conns.save(fromFile.concat(extra));
      }
    } catch (e) { /* keep the browser storage */ }
  }
  return DEFAULTS;
}

function showView(which) {
  $('dbview').classList.toggle('hidden', which !== 'list');
  $('lform').classList.toggle('hidden', which !== 'form');
}

async function showDbList(cancellable) {
  const d = await getDefaults();
  if (d.local_store) $('storeHint').textContent = 'Подключения и сохранённые пароли хранятся на этом компьютере, в базе программы (SQLite).';
  pendingDelete = null;
  renderDbList(d);
  $('dberr').textContent = '';
  $('dbclose').classList.toggle('hidden', !(cancellable && started));
  const box = $('login');
  box.classList.toggle('overlay', !!(cancellable && started));
  box.classList.remove('hidden');
  showView('list');
}
const showLogin = showDbList;   // api() calls it on 401

function dbRow({ id, name, desc, badges, actions, current }) {
  return `<div class="dbrow${current ? ' current' : ''}" role="listitem">
    <button type="button" class="dbopen" data-open="${esc(id)}" title="Подключиться">
      <span class="dbname">${esc(name)}</span>
      <span class="dbdesc mono">${esc(desc)}</span>
      <span class="dbbadges">${badges.join('')}</span>
    </button>
    <div class="dbacts">${actions}</div>
  </div>`;
}

function renderDbList(d) {
  const rows = [];
  const connected = new Set((S.targets || []).map((t) => t.name));
  // the server's own databases first (synthetic demo, then the real one), then the saved connections
  const srv = d.server_dbs || (d.demo_target ? [{ name: d.demo_target, synthetic: !d.host,
    desc: d.host ? `${d.user}@${d.host}:${d.port}/${d.service}` : '', load: d.server_load, load_available: d.server_load_available }] : []);
  for (const x of srv) {
    const openBadge = d.server_open ? '<span class="badge demo">Демо · без пароля</span>' : '<span class="badge">База сервера</span>';
    rows.push(dbRow({ id: `__server__:${x.name}`, name: x.name,
      desc: x.desc || (x.synthetic ? 'встроенная, работает без сети' : ''),
      badges: [`<span class="badge">${KIND_LABEL[x.kind] || 'Oracle'}</span>`, openBadge, x.synthetic ? '<span class="badge">синтетическая</span>' : '',
        x.load ? '<span class="badge load">нагрузка</span>' : ''],
      actions: x.load_available
        ? `<button type="button" data-srvload="${x.load ? 'off' : 'on'}" class="${x.load ? 'on' : ''}"
             title="Нагрузочное тестирование базы сервера">Нагрузка: ${x.load ? 'вкл' : 'выкл'}</button>` : '',
      current: connected.has(x.name) }));
  }
  const list = conns.all().sort((a, b) => (a.name || '').localeCompare(b.name || '', 'ru'));
  for (const c of list) {
    const del = pendingDelete === c.id
      ? `<button type="button" class="danger" data-del="${esc(c.id)}">Точно удалить?</button>`
      : `<button type="button" data-del="${esc(c.id)}">Удалить</button>`;
    rows.push(dbRow({ id: c.id, name: c.name || `${c.service}@${c.host}`, desc: connLabel(c).replace(/^\S+ · /, ''),
      badges: [`<span class="badge">${KIND_LABEL[connKind(c)]}</span>`,
        c.password ? '<span class="badge ok">пароль сохранён</span>' : '<span class="badge">нужен пароль</span>',
        c.load && c.load.enabled ? '<span class="badge load">нагрузка</span>' : ''],
      actions: `<button type="button" data-edit="${esc(c.id)}">Изменить</button>${del}`,
      current: c.target && connected.has(c.target) }));
  }
  if (!list.length) rows.push('<p class="dbempty">Сохранённых подключений пока нет. Нажмите «＋ Добавить базу».</p>');
  $('dblist').innerHTML = rows.join('');
}

$('dblist').addEventListener('click', async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.open) {
    ashvIntro.prime();                 // звук разрешён только в обработчике действия пользователя
    if (b.dataset.open.startsWith('__server__:')) return openServerDb(b.dataset.open.slice('__server__:'.length));
    const c = conns.get(b.dataset.open);
    if (!c) return;
    if (!c.password) return openForm(c, 'Введите пароль, чтобы подключиться');
    b.classList.add('busy');
    try { await connect(c); } catch (err) { openForm(c, err.message); } finally { b.classList.remove('busy'); }
  } else if (b.dataset.srvload) {
    b.disabled = true;
    try {
      const r = await fetch('/api/server-load', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: b.dataset.srvload === 'on' }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `Ошибка ${r.status}`);
      DEFAULTS.server_load = j.server_load;
      (DEFAULTS.server_dbs || []).forEach((x) => { if (x.load_available) x.load = j.server_load; });
      toast(j.server_load ? 'Нагрузочное тестирование базы сервера запущено — первые фазы появятся на графике через несколько секунд'
        : 'Нагрузочное тестирование базы сервера остановлено');
      if (started) loadTargets().catch(() => {});
    } catch (err) { $('dberr').textContent = err.message; }
    renderDbList(DEFAULTS);
  } else if (b.dataset.edit) {
    openForm(conns.get(b.dataset.edit));
  } else if (b.dataset.del) {
    const id = b.dataset.del;
    if (pendingDelete !== id) { pendingDelete = id; renderDbList(DEFAULTS); return; }
    const c = conns.get(id);
    conns.remove(id); pendingDelete = null;
    if (c && c.target) {
      fetch(`/api/disconnect?target=${encodeURIComponent(c.target)}`, { method: 'POST', credentials: 'same-origin' }).catch(() => {});
      if (S.target === c.target) S.target = null;
    }
    await loadTargets().catch(() => {});
    renderDbList(DEFAULTS);
  }
});

async function openServerDb(name) {
  const d = DEFAULTS;
  if (d.server_open) {                       // базы сервера: без пароля, учётные данные хранит сервер
    try {
      const r = await fetch('/api/connect-server', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: name }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `Ошибка ${r.status}`);
      return enter(j.target);
    } catch (err) { $('dberr').textContent = err.message; return; }
  }
  // not a demo server: the server database needs a password like any other — offer it as a new saved connection
  const sx = (d.server_dbs || []).find((x) => x.name === name) || {};
  openForm({ id: null, type: sx.kind || d.type, name: `${name || d.demo_target} (с паролем)`, host: d.host, port: d.port, service: d.service, user: d.user,
    source: d.source || 'vsession' }, 'Введите пароль базы сервера');
}

// form fields differ: Oracle — service, SYSDBA, V$SESSION/ASH source; PostgreSQL — database, port 5432
function applyFormKind(kind, keepPort) {
  const pgk = kind === 'postgres';
  $('f-service-label').firstChild.textContent = pgk ? 'База данных' : 'Сервис (service name)';
  $('f-service').placeholder = pgk ? 'postgres' : 'XE';
  $('f-orarow').classList.toggle('hidden', pgk);
  $('f-loadhint-ora').classList.toggle('hidden', pgk);
  $('f-loadhint-pg').classList.toggle('hidden', !pgk);
  $('f-privs').textContent = pgk
    ? 'Пользователю мониторинга нужна роль pg_monitor (GRANT pg_monitor TO …), иначе тексты и ожидания чужих сессий скрыты.'
    : 'Пользователю мониторинга нужны права CREATE SESSION и SELECT_CATALOG_ROLE (или SELECT ANY DICTIONARY).';
  if (!keepPort) {
    const p = +$('f-port').value;
    if (!p || p === 1521 || p === 5432) $('f-port').value = pgk ? 5432 : 1521;
    if (!editingId) $('ltitle').textContent = `Новое подключение к ${KIND_LABEL[kind]}`;
  }
}
$('f-type').addEventListener('change', () => applyFormKind($('f-type').value, false));

function openForm(c, message) {
  const f = $('lform'), d = DEFAULTS || {};
  editingId = c && c.id ? c.id : null;
  const kind = (c && c.type) || d.type || 'oracle';
  $('f-type').value = kind;
  applyFormKind(kind, true);
  $('ltitle').textContent = editingId ? 'Изменение подключения' : `Новое подключение к ${KIND_LABEL[kind]}`;
  $('f-name').value = (c && c.name) || '';
  $('f-host').value = (c && c.host) || d.host || '';
  $('f-port').value = (c && c.port) || (kind === (d.type || 'oracle') && d.port) || (kind === 'postgres' ? 5432 : 1521);
  $('f-service').value = (c && c.service) || d.service || '';
  $('f-user').value = (c && c.user) || d.user || '';
  $('f-password').value = '';
  $('f-password').placeholder = editingId && c.password ? 'сохранён — оставьте пустым, чтобы не менять' : '';
  $('f-remember').checked = editingId ? !!c.password || !c.target : true;
  $('f-mode').value = (c && c.mode) || '';
  $('f-source').value = (c && c.source) || d.source || 'vsession';
  $('f-load').checked = !!(c && c.load && c.load.enabled);   // по умолчанию выключено
  $('lerr').textContent = message || '';
  $('lsubmit').textContent = 'Подключиться';
  $('login').classList.remove('hidden');
  showView('form');
  ($('f-host').value ? $('f-password') : $('f-host')).focus();
}

$('dbadd').onclick = () => openForm(null);
$('dbclose').onclick = hideLogin;
$('lcancel').onclick = () => showDbList(started);
$('addDb').onclick = () => showDbList(true);
function hideLogin() { $('login').classList.add('hidden'); }

// Names are unique (case-insensitive, the server database included), and one account on one database
// is saved once: an existing connection is changed with «Изменить», not added again.
function duplicateOf(c) {
  const name = c.name.trim().toLowerCase();
  const others = conns.all().filter((x) => x.id !== c.id);
  const serverNames = ((DEFAULTS && DEFAULTS.server_dbs) || []).map((x) => x.name.toLowerCase())
    .concat([(DEFAULTS && DEFAULTS.demo_target || '').toLowerCase()]);
  if (serverNames.includes(name) || others.some((x) => (x.name || '').toLowerCase() === name))
    return `База с названием «${c.name}» уже есть — укажите другое название`;
  const same = others.find((x) => connKind(x) === connKind(c) && x.host.toLowerCase() === c.host.toLowerCase() && +x.port === +c.port
    && x.service.toLowerCase() === c.service.toLowerCase() && x.user.toLowerCase() === c.user.toLowerCase());
  if (same) return `Это подключение уже сохранено как «${same.name}» — измените его кнопкой «Изменить»`;
  return null;
}

async function postLogin(c) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 45000);
  const body = { type: connKind(c), host: c.host, port: c.port, service: c.service, user: c.user, password: c.password,
    mode: c.mode || '', source: c.source || 'vsession', load: c.load || { enabled: false } };
  const r = await fetch('/api/login', { method: 'POST', credentials: 'same-origin', signal: ctl.signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .catch((err) => { throw new Error(err.name === 'AbortError' ? 'Нет ответа от сервера, попробуйте ещё раз' : err.message); })
    .finally(() => clearTimeout(timer));
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `Ошибка ${r.status}`);
  DEFAULTS = null;            // the load switch may have changed the server database's load: re-read it
  return j;
}

async function connect(c) {          // a saved connection: reconnect without asking for the password
  const j = await postLogin(c);
  conns.put({ ...c, target: j.target });
  await enter(j.target, j.load_note);
}

$('lform').addEventListener('submit', async (e) => {
  e.preventDefault();
  ashvIntro.prime();
  const prev = editingId ? conns.get(editingId) : null;
  const typed = $('f-password').value;
  const password = typed || (prev && prev.password) || '';
  if (!password) { $('lerr').textContent = 'Введите пароль'; $('f-password').focus(); return; }
  const c = {
    id: editingId || `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    name: $('f-name').value.trim(), host: $('f-host').value.trim(), port: +$('f-port').value || 1521,
    service: $('f-service').value.trim(), user: $('f-user').value.trim(), password, type: $('f-type').value,
    mode: $('f-type').value === 'oracle' ? $('f-mode').value : '', source: $('f-source').value,
    load: { enabled: $('f-load').checked },
  };
  if (!c.name) c.name = `${c.type === 'postgres' ? c.service : c.service.toUpperCase()}@${c.host}`;
  const dup = duplicateOf(c);
  if (dup) { $('lerr').textContent = dup; return; }
  const btn = $('lsubmit');
  btn.disabled = true; btn.textContent = 'Подключение…'; $('lerr').textContent = '';
  try {
    const j = await postLogin(c);
    const remember = $('f-remember').checked;
    conns.put({ ...c, target: j.target, password: remember ? c.password : '' });   // same id: an edit replaces, never adds
    editingId = null;
    $('f-password').value = '';
    await enter(j.target, j.load_note);
  } catch (err) {
    $('lerr').textContent = err.message;
  } finally { btn.disabled = false; btn.textContent = 'Подключиться'; }
});

function toast(text) {        // short message that does not get overwritten by the chart caption
  const t = document.createElement('div');
  t.className = 'toast'; t.setAttribute('role', 'status'); t.textContent = text;
  document.body.append(t);
  setTimeout(() => t.classList.add('out'), 7000);
  setTimeout(() => t.remove(), 7600);
}

async function enter(target, note) {
  hideLogin();
  S.target = target; S.filters = {}; S.sel = null;
  try { localStorage.setItem('dbash.target', S.target); } catch (err) { /* ignore */ }
  await loadTargets();
  $('target').value = S.target;
  ashvIntro.play();          // заставка после каждого выбора базы — поверх загружающегося приложения
  if (note) toast(note);
  if (!started) start(); else refresh();
}

$('logout').onclick = async () => {
  await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
  location.reload();
};

async function start() {
  started = true;
  await loadTargets();
  await refresh();
  setInterval(() => { if (!document.hidden && $('login').classList.contains('hidden')) loadTargets().catch(() => {}); }, 5000);
  setInterval(() => { if (S.live && !document.hidden && $('modal').classList.contains('hidden') && $('login').classList.contains('hidden')) refresh(); }, 5000);
}

// ------------------------------------------------------------------ login journal
// Open on the user's own computer; on a server — with the administrator key from the link …/#admin=<key>.
const ADMIN_KEY = 'dbash.admin';
(function takeAdminKey() {
  const m = location.hash.match(/admin=([\w-]+)/);
  if (!m) return;
  try { sessionStorage.setItem(ADMIN_KEY, m[1]); } catch (e) { /* storage unavailable */ }
  history.replaceState(null, '', location.pathname + location.search);    // the key does not stay in the address bar
})();
const adminKey = () => { try { return sessionStorage.getItem(ADMIN_KEY) || ''; } catch (e) { return ''; } };
const LOG_EVENTS = { login: 'Вход по паролю', server: 'База сервера', demo: 'Демо-вход', logout: 'Выход', blocked: 'Блокировка' };

async function openLoginLog() {
  $('m-title').textContent = 'Журнал входов';
  $('m-filter').classList.add('hidden');
  $('m-body').innerHTML = `<div class="logbar">
      <label class="fld">Период <select id="lg-days"><option value="1">сутки</option><option value="7">7 дней</option>
        <option value="30">30 дней</option><option value="92" selected>3 месяца</option></select></label>
      <label class="fld">Событие <select id="lg-event"><option value="">все</option>${Object.entries(LOG_EVENTS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label>
      <label class="fld"><input type="checkbox" id="lg-fail"> только неудачные</label>
      <div class="spacer"></div><button id="lg-csv" title="Выгрузить журнал за выбранный период">Скачать CSV</button>
    </div><div id="lg-stats" class="muted small"></div><div id="lg-table" class="tbl"><div class="empty">Загрузка…</div></div>`;
  $('modal').classList.add('over-login');
  $('modal').classList.remove('hidden');
  const load = async () => {
    const u = new URL('/api/login-log', location.origin);
    u.searchParams.set('days', $('lg-days').value);
    if ($('lg-event').value) u.searchParams.set('event', $('lg-event').value);
    if ($('lg-fail').checked) u.searchParams.set('ok', '0');
    const r = await fetch(u, { credentials: 'same-origin', headers: { 'X-Admin-Token': adminKey() } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { $('lg-table').innerHTML = `<div class="empty">${esc(j.detail || r.status)}</div>`; return; }
    const st = j.stats;
    $('lg-stats').textContent = `Записей: ${st.total} (удачных ${st.ok}, неудачных ${st.failed}), разных IP: ${st.ips}. `
      + `Журнал хранится ${j.keep_days} дней${j.rows.length < st.total ? `; показаны последние ${j.rows.length}` : ''}.`;
    $('lg-table').innerHTML = !j.rows.length ? '<div class="empty">Записей нет</div>' : `<table><thead><tr>
      <th>Время</th><th>Событие</th><th>Результат</th><th>IP</th><th>База</th><th>Пользователь БД</th><th>Подключение</th><th>Сообщение</th></tr></thead><tbody>${
      j.rows.map((x) => `<tr class="${x.ok ? '' : 'fail'}"><td class="mono">${fmtTs(x.ts)}</td><td>${esc(LOG_EVENTS[x.event] || x.event)}</td>
        <td>${x.ok ? '<span class="badge ok">успешно</span>' : '<span class="badge bad">отказ</span>'}</td>
        <td class="mono" title="${esc(x.user_agent || '')}">${esc(x.ip || '')}</td><td>${esc(x.target || '')}${x.kind ? ` <span class="muted small">${esc(KIND_LABEL[x.kind] || x.kind)}</span>` : ''}</td>
        <td>${esc(x.db_user || '')}</td><td class="mono small">${esc(x.dsn || '')}</td><td><div class="txt" title="${esc(x.message || '')}">${esc(x.message || '')}</div></td></tr>`).join('')}</tbody></table>`;
  };
  ['lg-days', 'lg-event', 'lg-fail'].forEach((id) => { $(id).onchange = load; });
  $('lg-csv').onclick = async () => {
    const u = new URL('/api/login-log.csv', location.origin);
    u.searchParams.set('days', $('lg-days').value);
    const r = await fetch(u, { credentials: 'same-origin', headers: { 'X-Admin-Token': adminKey() } });
    if (!r.ok) { toast('Не удалось выгрузить журнал'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob()); a.download = `login_log_${new Date().toISOString().slice(0, 10)}.csv`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };
  await load();
}
$('logBtn').onclick = openLoginLog;
$('dblog').onclick = openLoginLog;
getDefaults().then((d) => {
  const show = !!(d.login_log_local || adminKey());
  $('logBtn').classList.toggle('hidden', !show);
  $('dblog').classList.toggle('hidden', !show);
}).catch(() => {});

(async function main() {
  const me = await (await fetch('/api/me', { credentials: 'same-origin' })).json();
  $('demoTag').classList.toggle('hidden', !me.demo);
  $('logout').classList.toggle('hidden', !!me.demo);  // в демо «выход» сразу же снова подключает
  if (me.demo && me.load && me.load.phase) $('demoTag').title += `\nТекущая нагрузка: ${me.load.phase}`;
  showDbList(false);                                  // при входе — список баз данных
})();
