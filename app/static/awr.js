'use strict';
// Performance report (Oracle: AWR-like; PostgreSQL: pg_stat_* snapshots): snapshots, rendering, recommendations, export.
// Uses globals from app.js: $, esc, api, S, openSql, WC_COLORS, fmtTs.

const AWR = { snaps: [], report: null };
const SEV = {
  critical: ['Критично', '#f85149'], warning: ['Внимание', '#d29922'],
  info: ['К сведению', '#58a6ff'], ok: ['Норма', '#3fb950'],
};
const nf = (v, d = 1) => (v === null || v === undefined || v === '' ? '—'
  : typeof v === 'number' ? v.toLocaleString('ru', { maximumFractionDigits: d }) : esc(v));

async function awrPost(path, params) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) u.searchParams.append(k, v);
  const r = await fetch(u, { method: 'POST', credentials: 'same-origin' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.status);
  return j;
}

async function openAwr() {
  $('awr').classList.remove('hidden');
  $('awr-target').textContent = S.target;
  await awrLoadSnaps();
  if (curKind() === 'oracle') awrNative(); else $('awr-native').innerHTML = '';
}
function closeAwr() { $('awr').classList.add('hidden'); }

async function awrLoadSnaps(selectLast) {
  const d = await api('/api/awr/snapshots', { target: S.target });
  AWR.snaps = d.snapshots;
  const opt = (s) => `<option value="${s.snap_id}">#${s.snap_id} · ${fmtTs(s.ts, false)}${s.manual ? ' · вручную' : ''}</option>`;
  const list = AWR.snaps.slice().reverse();
  $('awr-begin').innerHTML = list.map(opt).join('');
  $('awr-end').innerHTML = list.map(opt).join('');
  if (list.length >= 2) { $('awr-end').value = list[0].snap_id; $('awr-begin').value = list[1].snap_id; }
  $('awr-info').textContent = AWR.snaps.length
    ? `Снимков: ${AWR.snaps.length}. ${d.interval_min > 0 ? `Автоматически каждые ${d.interval_min} мин.` : 'Автоматические снимки выключены (snapshot_minutes = 0).'}`
    : 'Снимков ещё нет — нажмите «Снять снимок», подождите под нагрузкой и снимите второй.';
  if (selectLast && list.length >= 2) awrBuild();
}

async function awrSnapshot() {
  const b = $('awr-snap'); b.disabled = true; b.textContent = 'Снимок…';
  try { await awrPost('/api/awr/snapshot', { target: S.target }); await awrLoadSnaps(); }
  catch (e) { $('awr-info').textContent = 'Ошибка снимка: ' + e.message; }
  finally { b.disabled = false; b.textContent = 'Снять снимок'; }
}

async function awrBuild() {
  const begin = $('awr-begin').value, end = $('awr-end').value;
  if (!begin || !end) return;
  $('awr-body').innerHTML = '<div class="empty">Формирование отчёта…</div>';
  try {
    AWR.report = await api('/api/awr/report', { target: S.target, begin, end });
    $('awr-body').innerHTML = AWR.report.kind === 'postgres' ? renderAwrPg(AWR.report) : renderAwr(AWR.report);
    $('awr-dl').disabled = false;
  } catch (e) {
    $('awr-body').innerHTML = `<div class="empty">${esc(e.message.replace(/^\d+ /, '').replace(/^\{"detail":"|"\}$/g, ''))}</div>`;
  }
}

async function awrNative() {
  const box = $('awr-native');
  box.innerHTML = '';
  try {
    const n = await api('/api/awr/native', { target: S.target });
    if (!n.available) { box.innerHTML = `<span class="muted" title="${esc(n.reason)}">AWR Oracle недоступен ⓘ</span>`; return; }
    const o = n.snapshots.map((s) => `<option value="${s.snap_id}">${s.snap_id} · ${esc(s.end_time)}</option>`).join('');
    box.innerHTML = `AWR Oracle: <select id="awrn-b">${o}</select> — <select id="awrn-e">${o}</select>
      <button id="awrn-go">HTML-отчёт Oracle</button>`;
    $('awrn-b').selectedIndex = Math.min(1, n.snapshots.length - 1);
    $('awrn-go').onclick = () => {
      const u = new URL('/api/awr/native/report', location.origin);
      u.searchParams.set('target', S.target); u.searchParams.set('begin', $('awrn-b').value); u.searchParams.set('end', $('awrn-e').value);
      window.open(u, '_blank');
    };
  } catch (e) { box.textContent = ''; }
}

// ------------------------------------------------------------------ rendering
function tbl(cols, rows, empty = 'Нет данных') {
  if (!rows || !rows.length) return `<div class="muted small">${empty}</div>`;
  return `<table class="awr-t"><thead><tr>${cols.map((c) => `<th class="${c.num ? 'num' : ''}">${c.h}</th>`).join('')}</tr></thead><tbody>${
    rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${c.f ? c.f(r) : nf(r[c.k], c.d ?? 1)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const pctBar = (p, color = '#58a6ff') => (p == null ? '—'
  : `<span class="pbar"><i style="width:${Math.min(100, p)}%;background:${color}"></i></span> ${nf(p)}%`);
const sqlLink = (id) => `<a class="k mono" data-sql="${esc(id)}">${esc(id)}</a>`;
const sec = (id, title, body) => `<section class="awr-sec" id="${id}"><h3>${title}</h3>${body}</section>`;

const txt = (t) => `<div class="txt" title="${esc(t)}">${esc(t)}</div>`;
const wcDot = (c) => `<span style="color:${WC_COLORS[c] || '#ccc'}">■</span>`;

function renderRecs(recs) {
  return recs.map((r) => {
    const [label, color] = SEV[r.severity] || SEV.info;
    return `<div class="rec" style="border-left-color:${color}">
      <div class="rec-h"><span class="sev" style="background:${color}">${label}</span><span class="area">${esc(r.area)}</span><b>${esc(r.title)}</b></div>
      ${r.evidence.length ? `<ul class="ev">${r.evidence.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}
      ${r.advice.length ? `<div class="adv">${r.advice.map((a) => `<p>→ ${esc(a)}</p>`).join('')}</div>` : ''}
      ${r.sql_ids.length ? `<div class="ids">SQL: ${r.sql_ids.map(sqlLink).join(' ')}</div>` : ''}
      ${r.commands.length ? `<pre class="cmd">${esc(r.commands.join('\n'))}</pre>` : ''}
    </div>`;
  }).join('');
}

function renderAdvice(title, rows, cols) {
  if (!rows || !rows.length) return '';
  return `<div class="adv-t"><h4>${title}</h4>${tbl(cols, rows.map((r) => ({ ...r, _cur: Math.abs(r.size_factor - 1) < 0.01 })))}</div>`;
}

function renderAwr(R) {
  const h = R.header;
  const sqlCols = (extra) => [
    { h: 'SQL ID', f: (r) => sqlLink(r.sql_id) }, ...extra,
    { h: 'Выполн.', k: 'executions', num: true, d: 0 },
    { h: 'Время, с', k: 'elapsed_s', num: true }, { h: 'мс/вып.', k: 'ela_per_exec_ms', num: true },
    { h: '% DB time', k: 'pct_db_time', num: true },
    { h: 'Модуль', f: (r) => esc(r.module || '') },
    { h: 'Текст', f: (r) => `<div class="txt" title="${esc(r.text)}">${esc(r.text)}</div>` },
  ];
  const sqlTabs = [
    ['elapsed', 'По времени', []], ['cpu', 'По CPU', [{ h: 'CPU, с', k: 'cpu_s', num: true }]],
    ['gets', 'Логические чтения', [{ h: 'Чтений', k: 'gets', num: true, d: 0 }, { h: 'на вып.', k: 'gets_per_exec', num: true }, { h: '% всех', k: 'pct_gets', num: true }]],
    ['reads', 'Физические чтения', [{ h: 'Чтений', k: 'reads', num: true, d: 0 }, { h: '% всех', k: 'pct_reads', num: true }, { h: 'I/O, с', k: 'io_s', num: true }]],
    ['executions', 'Выполнения', [{ h: 'Строк', k: 'rows', num: true, d: 0 }]],
    ['parses', 'Разборы', [{ h: 'Разборов', k: 'parses', num: true, d: 0 }]],
    ['application', 'Блокировки (Application)', [{ h: 'Ожидание, с', k: 'app_s', num: true }]],
  ];
  const segNames = { 'logical reads': 'Логические чтения', 'physical reads': 'Физические чтения',
    'row lock waits': 'Ожидания блокировок строк', 'buffer busy waits': 'Buffer busy waits',
    'ITL waits': 'ITL waits', 'db block changes': 'Изменения блоков', 'physical writes': 'Физические записи',
    'physical reads direct': 'Прямые чтения' };
  const counts = R.recommendations.reduce((a, r) => (a[r.severity] = (a[r.severity] || 0) + 1, a), {});
  const m = R.memory, adv = m.advice || {};
  const toc = [['awr-recs', 'Рекомендации'], ['awr-load', 'Профиль нагрузки'], ['awr-events', 'Ожидания'],
    ['awr-sql', 'SQL'], ['awr-seg', 'Сегменты'], ['awr-io', 'Ввод-вывод'], ['awr-mem', 'Память'],
    ['awr-inst', 'Экземпляр'], ['awr-par', 'Параметры']];

  return `
  <div class="awr-head">
    <div class="kv">
      <div><span>База / экземпляр</span>${esc(h.db_name)} / ${esc(h.instance)}</div>
      <div><span>Хост</span>${esc(h.host)}</div>
      <div><span>Версия</span>${esc(h.banner || h.version)}</div>
      <div><span>CPU</span>${h.cpus}</div>
      <div><span>Снимки</span>#${h.begin.id} ${fmtTs(h.begin.ts, false)} → #${h.end.id} ${fmtTs(h.end.ts, false)}</div>
      <div><span>Интервал</span>${nf(h.elapsed_min)} мин</div>
      <div><span>DB time</span>${nf(h.db_time_min, 2)} мин</div>
      <div><span>Средняя активность (AAS)</span>${nf(h.aas, 2)}</div>
      <div><span>Сессии (всего / активные)</span>${nf(h.sessions, 0)} / ${nf(h.active_sessions, 0)}</div>
    </div>
    <nav class="awr-toc">${toc.map(([id, t]) => `<a href="#${id}" data-goto="${id}">${t}</a>`).join('')}</nav>
  </div>
  ${sec('awr-recs', `Рекомендации <small>${Object.entries(counts).map(([k, v]) => `<span class="sev" style="background:${SEV[k][1]}">${SEV[k][0]}: ${v}</span>`).join(' ')}</small>`, renderRecs(R.recommendations))}
  ${sec('awr-load', 'Профиль нагрузки', `<div class="cols">
    <div>${tbl([{ h: 'Показатель', k: 'name' }, { h: 'в секунду', k: 'per_sec', num: true, d: 2 }, { h: 'на транзакцию', k: 'per_txn', num: true, d: 2 }, { h: 'всего', k: 'total', num: true, d: 0 }], R.load_profile)}</div>
    <div><h4>Эффективность экземпляра</h4>${tbl([{ h: 'Показатель', k: 'name' },
      { h: '%', num: true, f: (r) => (r.value == null ? '—' : `<span style="color:${r.value >= r.good ? '#3fb950' : '#d29922'}">${nf(r.value, 2)}</span>`) },
      { h: 'норма ≥', k: 'good', num: true }], R.efficiency)}
      <h4>ОС</h4>${tbl([{ h: 'CPU', k: 'num_cpus', num: true }, { h: 'Занят, %', k: 'busy_pct', num: true }, { h: 'user %', k: 'user_pct', num: true }, { h: 'sys %', k: 'sys_pct', num: true }, { h: 'iowait %', k: 'iowait_pct', num: true }, { h: 'Load', k: 'load', num: true, d: 2 }, { h: 'Память, МБ', k: 'memory_mb', num: true, d: 0 }], [R.os])}
    </div></div>`)}
  ${sec('awr-events', 'Главные события (Top 10 Timed Events)', `
    ${tbl([{ h: 'Событие', f: (r) => `<span style="color:${WC_COLORS[r.wait_class] || '#ccc'}">■</span> ${esc(r.event)}` },
      { h: 'Класс', k: 'wait_class' }, { h: 'Ожиданий', k: 'waits', num: true, d: 0 }, { h: 'Время, с', k: 'time_s', num: true, d: 2 },
      { h: 'Среднее, мс', k: 'avg_ms', num: true, d: 2 }, { h: '% DB time', f: (r) => pctBar(r.pct_db_time, WC_COLORS[r.wait_class]) }], R.top_events)}
    <div class="cols"><div><h4>Классы ожиданий</h4>${tbl([{ h: 'Класс', f: (r) => `<span style="color:${WC_COLORS[r.wait_class] || '#ccc'}">■</span> ${esc(r.wait_class)}` },
      { h: 'Время, с', k: 'time_s', num: true, d: 2 }, { h: '% DB time', f: (r) => pctBar(r.pct_db_time, WC_COLORS[r.wait_class]) }], R.wait_classes)}</div>
    <div><h4>Модель времени</h4>${tbl([{ h: 'Статистика', k: 'name' }, { h: 'Время, с', k: 'time_s', num: true, d: 2 }, { h: '% DB time', k: 'pct_db_time', num: true }], R.time_model.slice(0, 14))}</div></div>
    <details><summary>Все события ожидания (${R.events.length})</summary>${tbl([{ h: 'Событие', k: 'event' }, { h: 'Класс', k: 'wait_class' }, { h: 'Ожиданий', k: 'waits', num: true, d: 0 }, { h: 'Время, с', k: 'time_s', num: true, d: 3 }, { h: 'Среднее, мс', k: 'avg_ms', num: true, d: 2 }], R.events)}</details>`)}
  ${sec('awr-sql', 'SQL', sqlTabs.map(([k, t, extra]) => `<details ${k === 'elapsed' ? 'open' : ''}><summary>${t} (${(R.sql[k] || []).length})</summary>${tbl(sqlCols(extra), R.sql[k])}</details>`).join(''))}
  ${sec('awr-seg', 'Сегменты', Object.keys(R.segments).length ? Object.entries(R.segments).map(([k, rows]) =>
    `<details ${k === 'logical reads' ? 'open' : ''}><summary>${segNames[k] || k}</summary>${tbl([{ h: 'Владелец', k: 'owner' }, { h: 'Объект', k: 'object' }, { h: 'Тип', k: 'type' }, { h: 'Табл. пр-во', k: 'tablespace' }, { h: 'Значение', k: 'value', num: true, d: 0 }, { h: '% всех', k: 'pct', num: true }], rows)}</details>`).join('') : '<div class="muted small">Нет данных</div>')}
  ${sec('awr-io', 'Ввод-вывод по табличным пространствам', tbl([{ h: 'Табл. пространство', k: 'tablespace' }, { h: 'Тип', k: 'kind' }, { h: 'Чтений', k: 'reads', num: true, d: 0 }, { h: 'в сек', k: 'reads_per_s', num: true, d: 2 }, { h: '<span title="V$FILESTAT считает время с точностью 10 мс: 0 означает «быстрее 10 мс»">Среднее, мс ⓘ</span>', k: 'avg_read_ms', num: true, d: 2 }, { h: 'Блоков/чтение', k: 'blocks_per_read', num: true }, { h: 'Записей', k: 'writes', num: true, d: 0 }, { h: 'Запись, мс', k: 'avg_write_ms', num: true, d: 2 }], R.io))}
  ${sec('awr-mem', 'Память и советники', `<div class="cols">
    <div><h4>SGA</h4>${tbl([{ h: 'Компонент', k: 'name' }, { h: 'МБ', k: 'mb', num: true }], m.sga)}
      <h4>PGA</h4>${tbl([{ h: 'Показатель', k: 'name' }, { h: 'Значение', f: (r) => (/target|allocated|inuse/.test(r.name) && !/count/.test(r.name) ? nf(r.value / 1048576) + ' МБ' : nf(r.value)) , num: true }], m.pga)}</div>
    <div class="advs">
      ${renderAdvice('Советник SGA', adv.sga, [{ h: 'SGA, МБ', f: (r) => (r._cur ? `<b>${r.size_mb} ◄</b>` : r.size_mb), num: true }, { h: 'Множитель', k: 'size_factor', num: true, d: 2 }, { h: 'DB time (оценка)', k: 'db_time', num: true, d: 0 }, { h: 'Физ. чтения', k: 'reads', num: true, d: 0 }])}
      ${renderAdvice('Советник буферного кэша', adv.db_cache, [{ h: 'Кэш, МБ', f: (r) => (r._cur ? `<b>${r.size_mb} ◄</b>` : r.size_mb), num: true }, { h: 'Множитель', k: 'size_factor', num: true, d: 2 }, { h: 'Чтения ×', k: 'read_factor', num: true, d: 3 }])}
      ${renderAdvice('Советник shared pool', adv.shared_pool, [{ h: 'Shared pool, МБ', f: (r) => (r._cur ? `<b>${r.size_mb} ◄</b>` : r.size_mb), num: true }, { h: 'Множитель', k: 'size_factor', num: true, d: 2 }, { h: 'Сэкономлено, с', k: 'time_saved_s', num: true, d: 0 }])}
      ${renderAdvice('Советник PGA', adv.pga, [{ h: 'PGA, МБ', f: (r) => (r._cur ? `<b>${r.size_mb} ◄</b>` : r.size_mb), num: true }, { h: 'Множитель', k: 'size_factor', num: true, d: 3 }, { h: 'Hit, %', k: 'hit_pct', num: true }, { h: 'Over-alloc', k: 'overalloc', num: true, d: 0 }])}
    </div></div>`)}
  ${sec('awr-inst', 'Экземпляр', `<div class="cols">
    <div><h4>Redo</h4>${tbl([{ h: 'Переключений', k: 'count', num: true, d: 0 }, { h: 'в час', k: 'per_hour', num: true }], [R.log_switches])}
      ${tbl([{ h: 'Группа', k: 'group#', num: true, d: 0 }, { h: 'Размер, МБ', f: (r) => nf(r.bytes / 1048576, 0), num: true }, { h: 'Членов', k: 'members', num: true, d: 0 }, { h: 'Статус', k: 'status' }], R.log_switches.logs)}
      <h4>Undo</h4>${tbl([{ h: 'Самый долгий запрос, с', k: 'max_query_s', num: true, d: 0 }, { h: 'ORA-01555', k: 'snapshot_too_old', num: true, d: 0 }, { h: 'Нет места', k: 'no_space', num: true, d: 0 }], [R.undo])}</div>
    <div><h4>Лимиты ресурсов</h4>${tbl([{ h: 'Ресурс', k: 'resource_name' }, { h: 'Сейчас', k: 'cur', num: true, d: 0 }, { h: 'Максимум', k: 'max_used', num: true, d: 0 }, { h: 'Лимит', k: 'limit_value', num: true }], R.limits)}
      <h4>Статистика оптимизатора</h4>${tbl([{ h: 'Схема', k: 'owner' }, { h: 'Устарела', k: 'stale', num: true, d: 0 }, { h: 'Нет', k: 'never', num: true, d: 0 }], R.stale_stats, 'Статистика актуальна')}</div></div>`)}
  ${sec('awr-par', 'Параметры', tbl([{ h: 'Параметр', k: 'name' }, { h: 'Значение', f: (r) => `<span class="mono">${esc(r.value ?? '')}</span>` }, { h: '', f: (r) => (r.default ? '' : '<span class="muted small">изменён</span>') }], R.params))}
  <p class="muted small">Отчёт построен по собственным снимкам V$-статистики (как Statspack), без DBA_HIST и Diagnostics Pack.</p>`;
}

// ------------------------------------------------------------------ PostgreSQL report
function renderSqlPg(Q) {
  const ashTbl = tbl([{ h: 'Запрос', f: (r) => sqlLink(r.sql_id) }, { h: 'Тип', k: 'op' }, { h: 'База', k: 'db' },
    { h: 'Сэмплов', k: 'samples', num: true, d: 0 }, { h: 'AAS', k: 'aas', num: true, d: 3 }, { h: '% активности', k: 'pct', num: true },
    { h: 'CPU', k: 'cpu', num: true, d: 0 }, { h: 'IO', k: 'io', num: true, d: 0 }, { h: 'Lock', k: 'lock', num: true, d: 0 },
    { h: 'Текст', f: (r) => txt(r.text) }], Q.ash, 'Активных запросов в интервале не было');
  if (Q.source !== 'pg_stat_statements') {
    return `<p class="muted small">${esc(Q.note || 'pg_stat_statements недоступно')} — запросы показаны по выборкам активных сессий.</p>${ashTbl}`;
  }
  const cols = (extra) => [
    { h: 'Запрос', f: (r) => sqlLink(r.sql_id) }, ...extra,
    { h: 'Выполн.', k: 'executions', num: true, d: 0 }, { h: 'Время, с', k: 'elapsed_s', num: true, d: 2 },
    { h: 'мс/вып.', k: 'ela_per_exec_ms', num: true, d: 2 }, { h: '% времени', k: 'pct_time', num: true },
    { h: 'База', f: (r) => esc(r.module || '') }, { h: 'Текст', f: (r) => txt(r.text) },
  ];
  const tabs = [
    ['elapsed', 'По общему времени', []],
    ['mean', 'По времени одного выполнения', []],
    ['executions', 'Выполнения', [{ h: 'Строк', k: 'rows', num: true, d: 0 }]],
    ['reads', 'Чтения с диска', [{ h: 'Блоков с диска', k: 'reads', num: true, d: 0 }, { h: '% всех', k: 'pct_reads', num: true }, { h: 'I/O, с', k: 'io_s', num: true, d: 2 }]],
    ['gets', 'Все чтения блоков', [{ h: 'Блоков', k: 'gets', num: true, d: 0 }, { h: 'Hit, %', k: 'hit_pct', num: true }]],
    ['temp', 'Временные файлы', [{ h: 'Временных блоков', k: 'temp_blks', num: true, d: 0 }]],
  ];
  return tabs.map(([k, t, extra]) => `<details ${k === 'elapsed' ? 'open' : ''}><summary>${t} (${(Q[k] || []).length})</summary>${tbl(cols(extra), Q[k])}</details>`).join('')
    + `<details><summary>По выборкам активных сессий (${(Q.ash || []).length})</summary>${ashTbl}</details>`;
}

function renderAwrPg(R) {
  const h = R.header, T = R.tables, I = R.instance;
  const counts = R.recommendations.reduce((a, r) => (a[r.severity] = (a[r.severity] || 0) + 1, a), {});
  const toc = [['awr-recs', 'Рекомендации'], ['awr-load', 'Профиль нагрузки'], ['awr-events', 'Ожидания'],
    ['awr-sql', 'SQL'], ['awr-tab', 'Таблицы и индексы'], ['awr-db', 'Базы данных'], ['awr-inst', 'Сервер'],
    ['awr-par', 'Параметры']];
  const tcols = (extra) => [{ h: 'Таблица', k: 'table' }, ...extra, { h: 'Размер, МБ', k: 'size_mb', num: true }];

  return `
  <div class="awr-head">
    <div class="kv">
      <div><span>База / кластер</span>${esc(h.db_name)} / ${esc(h.instance)}</div>
      <div><span>Хост</span>${esc(h.host)}</div>
      <div><span>Версия</span>PostgreSQL ${esc(h.version)}</div>
      <div><span>Запущен</span>${esc(String(h.startup_time || '').slice(0, 19))}</div>
      <div><span>Снимки</span>#${h.begin.id} ${fmtTs(h.begin.ts, false)} → #${h.end.id} ${fmtTs(h.end.ts, false)}</div>
      <div><span>Интервал</span>${nf(h.elapsed_min)} мин</div>
      <div><span>Активность (DB time ≈)</span>${nf(h.db_time_min, 2)} мин</div>
      <div><span>Средняя активность (AAS)</span>${nf(h.aas, 2)}${h.cpus ? ` при ${h.cpus} CPU` : ''}</div>
      <div><span>Соединения (всего / активные / лимит)</span>${nf(h.sessions, 0)} / ${nf(h.active_sessions, 0)} / ${nf(h.max_connections, 0)}</div>
    </div>
    <nav class="awr-toc">${toc.map(([id, t]) => `<a href="#${id}" data-goto="${id}">${t}</a>`).join('')}</nav>
  </div>
  ${sec('awr-recs', `Рекомендации <small>${Object.entries(counts).map(([k, v]) => `<span class="sev" style="background:${SEV[k][1]}">${SEV[k][0]}: ${v}</span>`).join(' ')}</small>`, renderRecs(R.recommendations))}
  ${sec('awr-load', 'Профиль нагрузки', `<div class="cols">
    <div>${tbl([{ h: 'Показатель', k: 'name' }, { h: 'в секунду', k: 'per_sec', num: true, d: 2 }, { h: 'на транзакцию', k: 'per_txn', num: true, d: 2 }, { h: 'всего', k: 'total', num: true, d: 0 }], R.load_profile)}</div>
    <div><h4>Эффективность</h4>${tbl([{ h: 'Показатель', k: 'name' },
      { h: '%', num: true, f: (r) => (r.value == null ? '—' : `<span style="color:${r.value >= r.good ? '#3fb950' : '#d29922'}">${nf(r.value, 2)}</span>`) },
      { h: 'норма ≥', k: 'good', num: true }], R.efficiency)}
    </div></div>`)}
  ${sec('awr-events', 'Главные ожидания (по выборкам активных сессий)', `
    ${tbl([{ h: 'Событие', f: (r) => `${wcDot(r.wait_class)} ${esc(r.event)}` },
      { h: 'Класс', k: 'wait_class' }, { h: 'Сэмплов', k: 'samples', num: true, d: 0 }, { h: 'Время ≈, с', k: 'time_s', num: true, d: 0 },
      { h: '% активности', f: (r) => pctBar(r.pct_db_time, WC_COLORS[r.wait_class]) }], R.top_events, 'Нет выборок за интервал')}
    <div class="cols"><div><h4>Классы ожиданий</h4>${tbl([{ h: 'Класс', f: (r) => `${wcDot(r.wait_class)} ${esc(r.wait_class)}` },
      { h: 'Время ≈, с', k: 'time_s', num: true, d: 0 }, { h: '% активности', f: (r) => pctBar(r.pct_db_time, WC_COLORS[r.wait_class]) }], R.wait_classes)}</div>
    <div class="muted small" style="align-self:center">PostgreSQL не измеряет длительность ожиданий: время оценено по ежесекундным выборкам pg_stat_activity — одна выборка активной сессии ≈ 1 секунда её работы.</div></div>
    <details><summary>Все события (${R.events.length})</summary>${tbl([{ h: 'Событие', k: 'event' }, { h: 'Класс', k: 'wait_class' }, { h: 'Сэмплов', k: 'samples', num: true, d: 0 }, { h: '%', k: 'pct_db_time', num: true }], R.events)}</details>`)}
  ${sec('awr-sql', 'SQL', renderSqlPg(R.sql))}
  ${sec('awr-tab', `Таблицы и индексы <small class="muted">база ${esc(T.db)}</small>`, `
    <details open><summary>Последовательное чтение строк (${T.seq_tup_read.length})</summary>${tbl(tcols([{ h: 'Seq scan', k: 'seq_scan', num: true, d: 0 }, { h: 'Строк прочитано', k: 'seq_tup_read', num: true, d: 0 }, { h: 'Index scan', k: 'idx_scan', num: true, d: 0 }]), T.seq_tup_read)}</details>
    <details><summary>Чтения с диска (${T.heap_blks_read.length})</summary>${tbl(tcols([{ h: 'Блоков таблицы', k: 'heap_blks_read', num: true, d: 0 }, { h: 'Блоков индексов', k: 'idx_blks_read', num: true, d: 0 }]), T.heap_blks_read)}</details>
    <details><summary>Изменения строк (${T.modified.length})</summary>${tbl(tcols([{ h: 'Вставлено', k: 'n_tup_ins', num: true, d: 0 }, { h: 'Изменено', k: 'n_tup_upd', num: true, d: 0 }, { h: 'из них HOT', k: 'n_tup_hot_upd', num: true, d: 0 }, { h: 'Удалено', k: 'n_tup_del', num: true, d: 0 }]), T.modified)}</details>
    <details><summary>«Мёртвые» строки (${T.dead.length})</summary>${tbl(tcols([{ h: 'Живых', k: 'live', num: true, d: 0 }, { h: 'Мёртвых', k: 'dead', num: true, d: 0 }, { h: '%', k: 'dead_pct', num: true }, { h: 'Автоочистка', f: (r) => esc(String(r.last_autovacuum || '—').slice(0, 19)) }, { h: 'Анализ', f: (r) => esc(String(r.last_autoanalyze || '—').slice(0, 19)) }]), T.dead, 'Мёртвых строк мало')}</details>
    <details><summary>Неиспользуемые индексы (${T.unused_indexes.length})</summary>${tbl([{ h: 'Индекс', f: (r) => esc(`${r.schemaname}.${r.index_name}`) }, { h: 'Таблица', k: 'table_name' }, { h: 'Размер, МБ', f: (r) => nf(r.size / 1048576), num: true }], T.unused_indexes, 'Нет индексов без сканирований крупнее 1 МБ')}</details>`)}
  ${sec('awr-db', 'Базы данных', tbl([{ h: 'База', k: 'datname' }, { h: 'Размер, МБ', k: 'size_mb', num: true }, { h: 'Соединений', k: 'backends', num: true, d: 0 },
    { h: 'TPS', k: 'tps', num: true, d: 2 }, { h: 'Откатов', k: 'rollbacks', num: true, d: 0 }, { h: 'Hit, %', k: 'hit_pct', num: true },
    { h: 'С диска, блоков', k: 'reads', num: true, d: 0 }, { h: 'Временные, МБ', k: 'temp_mb', num: true }, { h: 'Deadlocks', k: 'deadlocks', num: true, d: 0 },
    { h: 'Возраст XID', k: 'xid_age', num: true, d: 0 }], R.databases))}
  ${sec('awr-inst', 'Сервер', `<div class="cols">
    <div><h4>Соединения: ${nf(I.connections, 0)} из ${nf(I.max_connections, 0)}</h4>${tbl([{ h: 'Состояние', k: 'state' }, { h: 'Сессий', k: 'sessions', num: true, d: 0 }, { h: 'Ждут блокировок', k: 'lock_waits', num: true, d: 0 }, { h: 'Дольше всех в состоянии, с', k: 'longest_in_state_s', num: true, d: 0 }], I.by_state)}
      <h4>Долгие транзакции</h4>${tbl([{ h: 'pid', k: 'pid', d: 0 }, { h: 'Пользователь', k: 'usename' }, { h: 'База', k: 'datname' }, { h: 'Состояние', k: 'state' }, { h: 'Длится, с', k: 'xact_s', num: true, d: 0 }, { h: 'Запрос', f: (r) => txt(r.query) }], I.long_xacts, 'Нет транзакций дольше 5 минут')}</div>
    <div><h4>Контрольные точки и WAL</h4>${tbl([{ h: 'По времени', k: 'timed', num: true, d: 0 }, { h: 'По требованию', k: 'requested', num: true, d: 0 }, { h: 'в час', k: 'per_hour', num: true }, { h: 'Запись, с', k: 'write_s', num: true }, { h: 'Sync, с', k: 'sync_s', num: true }], [I.checkpoints])}
      <p class="small">WAL: ${nf(I.wal_mb_per_hour)} МБ/ч</p>
      <h4>Слоты репликации</h4>${tbl([{ h: 'Слот', k: 'slot_name' }, { h: 'Тип', k: 'slot_type' }, { h: 'Активен', f: (r) => (r.active ? 'да' : '<b>нет</b>') }, { h: 'Удержано WAL, МБ', f: (r) => nf((r.retained_bytes || 0) / 1048576), num: true }], I.slots, 'Слотов нет')}
      <h4>Реплики</h4>${tbl([{ h: 'Приложение', k: 'application_name' }, { h: 'Адрес', k: 'client' }, { h: 'Состояние', k: 'state' }, { h: 'Отставание, МБ', f: (r) => nf((r.lag_bytes || 0) / 1048576), num: true }], I.replication, 'Реплик нет')}
      <p class="small muted">Расширения: ${esc((I.extensions || []).join(', ') || '—')}</p></div></div>`)}
  ${sec('awr-par', 'Параметры', tbl([{ h: 'Параметр', k: 'name' }, { h: 'Значение', f: (r) => `<span class="mono">${esc(r.value ?? '')}</span>` }, { h: 'Источник', f: (r) => (r.default ? '' : `<span class="muted small">${esc(r.source || 'изменён')}</span>`) }, { h: 'Описание', f: (r) => `<span class="muted small">${esc(r.desc || '')}</span>` }], R.params))}
  <p class="muted small">Отчёт построен по собственным снимкам накопительной статистики PostgreSQL (pg_stat_*) и ежесекундным выборкам pg_stat_activity.</p>`;
}

// ------------------------------------------------------------------ export
const AWR_EXPORT_CSS = `body{font:13px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2328;background:#fff;margin:24px;max-width:1200px}
h1{font-size:20px}h3{font-size:16px;border-bottom:2px solid #d0d7de;padding-bottom:4px;margin-top:28px}h4{margin:14px 0 6px}
table{border-collapse:collapse;width:100%;margin:6px 0}th,td{border-bottom:1px solid #e5e7eb;padding:3px 6px;text-align:left;vertical-align:top}
th{background:#f6f8fa}td.num,th.num{text-align:right}.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:6px 18px}.kv span{display:block;color:#57606a;font-size:11px}
.rec{border-left:4px solid #999;background:#f6f8fa;padding:8px 12px;margin:8px 0;border-radius:4px}.sev{color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;margin-right:6px}
.area{color:#57606a;margin-right:6px}.cmd{background:#0d1117;color:#e6edf3;padding:8px;border-radius:4px;white-space:pre-wrap}.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.txt{max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:Menlo,Consolas,monospace;font-size:11px}.mono{font-family:Menlo,Consolas,monospace}
.pbar{display:inline-block;width:70px;height:8px;background:#eee}.pbar i{display:block;height:100%}.awr-toc{display:none}a{color:#0969da}.muted{color:#57606a}.small{font-size:11px}details>summary{cursor:pointer;font-weight:600;margin:6px 0}`;

function awrDownload() {
  if (!AWR.report) return;
  const h = AWR.report.header;
  const body = $('awr-body').cloneNode(true);
  body.querySelectorAll('details').forEach((d) => d.setAttribute('open', ''));
  const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>AWR ${esc(h.db_name)} ${h.begin.id}-${h.end.id}</title>
<style>${AWR_EXPORT_CSS}</style></head><body><h1>Отчёт о производительности ${AWR.report.kind === 'postgres' ? 'PostgreSQL ' : ''}${esc(h.db_name)} (${esc(h.instance || h.host)})</h1>${body.innerHTML}</body></html>`;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
  a.download = `${AWR.report.kind === 'postgres' ? 'pg_report' : 'awr'}_${h.db_name}_${h.begin.id}_${h.end.id}.html`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

$('awrBtn').onclick = openAwr;
$('awr-close').onclick = closeAwr;
$('awr-snap').onclick = awrSnapshot;
$('awr-go').onclick = awrBuild;
$('awr-dl').onclick = awrDownload;
$('awr-body').addEventListener('click', (e) => {
  const g = e.target.closest('[data-goto]');
  if (g) { e.preventDefault(); document.getElementById(g.dataset.goto)?.scrollIntoView({ behavior: 'smooth' }); }
});
// capture phase: decide before app.js closes the SQL modal on the same Escape
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('modal').classList.contains('hidden')) closeAwr(); }, true);
