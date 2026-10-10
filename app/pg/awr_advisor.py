"""Rule-based tuning advice for the PostgreSQL report (see awr.py).

advise(report_obj, report_dict) -> list of findings:
  {"severity": "critical"|"warning"|"info"|"ok", "area": str, "title": str,
   "evidence": [str], "advice": [str], "commands": [str], "sql_ids": [str]}
Thresholds are deliberately conservative: a finding needs both a ratio and a meaningful amount of work.
"""
from __future__ import annotations

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
MB = 1048576


def _f(v, nd=1):
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return f"{v:,.{nd}f}".replace(",", " ")


def _size(b):
    if b is None:
        return "—"
    for unit, k in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if abs(b) >= k:
            return f"{b / k:,.1f} {unit}".replace(",", " ")
    return f"{int(b)} Б"


class Advisor:
    def __init__(self, rpt, rep):
        self.r, self.rep = rpt, rep
        self.out = []
        self.h = rep["header"]
        self.eff = {e["name"]: e["value"] for e in rep["efficiency"]}
        self.wc = {w["wait_class"]: w for w in rep["wait_classes"]}
        self.ev = {e["event"]: e for e in rep["events"]}
        self.version = int(rpt.inst.get("version_num") or 0)

    def add(self, severity, area, title, evidence=(), advice=(), commands=(), sql_ids=()):
        self.out.append({"severity": severity, "area": area, "title": title, "evidence": list(evidence),
                         "advice": list(advice), "commands": list(commands), "sql_ids": list(sql_ids)})

    def share(self, wait_class):
        return (self.wc.get(wait_class) or {}).get("pct_db_time") or 0.0

    def top_sql(self, key, n=3):
        sql = self.rep["sql"]
        rows = sql.get(key) if sql.get("source") == "pg_stat_statements" else sql.get("ash")
        return [s["sql_id"] for s in (rows or [])[:n] if s.get("sql_id")]

    # ------------------------------------------------------------------ rules

    def activity(self):
        h = self.h
        if not h["ticks"]:
            self.add("warning", "Нагрузка", "За интервал нет истории активных сессий",
                     ["Сборщик не работал между снимками: ожидания и активные SQL неизвестны"],
                     ["Снимайте снимки, пока приложение подключено к базе, — профиль ожиданий строится по "
                      "ежесекундным выборкам pg_stat_activity."])
            return
        if (h["aas"] or 0) < 0.02 and self.r.txn < 100:
            self.add("info", "Нагрузка", "Нагрузка в интервале низкая — выводы ограничены",
                     [f"Средняя активность (AAS) {h['aas']}, транзакций {_f(self.r.txn)} за {_f(h['elapsed_min'])} мин"],
                     ["Для анализа выберите снимки, охватывающие период типичной или пиковой нагрузки."])

    def cpu(self):
        cpus = self.r.cpus
        aas_cpu = (self.wc.get("CPU") or {}).get("samples", 0) / max(1, self.h["ticks"] or 1)
        if cpus and aas_cpu > 0.8 * cpus:
            self.add("critical" if aas_cpu > cpus else "warning", "CPU", "Процессор — узкое место",
                     [f"В среднем {_f(aas_cpu, 2)} сессий на CPU при {cpus} ядрах"],
                     ["Найдите запросы с наибольшим временем (раздел SQL) и снизьте их стоимость: индексы, "
                      "переписывание, кэширование результатов.",
                      "Проверьте, не выросло ли число одновременных соединений: пул соединений (PgBouncer) "
                      "держит число активных сессий около числа ядер."],
                     sql_ids=self.top_sql("elapsed"))

    def cache(self):
        hit = self.eff.get("Попадание в кэш (Cache Hit %)")
        reads = self.r.tot["blks_read"]
        sb = self.r.setting_bytes("shared_buffers")
        if hit is not None and hit < 99 and reads > 10000:
            sev = "warning" if hit < 95 else "info"
            ev = [f"Попадание в кэш {_f(hit, 2)}%, чтений с диска {_f(reads)} блоков "
                  f"({_size(reads * self.r.block)})"]
            if sb:
                ev.append(f"shared_buffers = {_size(sb)}")
            adv = ["Чтения с диска чаще всего вызывают несколько запросов без подходящего индекса — "
                   "начните с SQL по физическим чтениям и таблиц с последовательными сканированиями."]
            cmds = []
            if sb and sb < 1 << 30:
                adv.append("shared_buffers обычно задают около 25% оперативной памяти сервера "
                           "(effective_cache_size — 50–75%). Требуется перезапуск.")
                cmds.append("ALTER SYSTEM SET shared_buffers = '2GB';   -- ~25% ОЗУ; затем перезапуск")
            self.add(sev, "Память", "Низкое попадание в буферный кэш", ev, adv, cmds, self.top_sql("reads"))
        if sb is not None and sb <= 128 * MB:
            self.add("warning", "Параметры", f"shared_buffers = {_size(sb)}: значение для минимальных систем",
                     [f"shared_buffers = {_size(sb)}"],
                     ["Значение по умолчанию рассчитано на минимальные системы. Для выделенного сервера — около "
                      "25% ОЗУ; effective_cache_size — 50–75% ОЗУ (подсказка планировщику, без выделения памяти)."],
                     ["ALTER SYSTEM SET shared_buffers = '2GB';",
                      "ALTER SYSTEM SET effective_cache_size = '6GB';",
                      "-- shared_buffers применяется после перезапуска PostgreSQL"])

    def temp(self):
        t = self.r.tot
        wm = self.r.setting_bytes("work_mem")
        if t["temp_files"] > 0 and t["temp_bytes"] > 50 * MB:
            per_h = t["temp_bytes"] * 3600 / self.r.elapsed
            sev = "warning" if per_h > 1 << 30 else "info"
            self.add(sev, "Память", "Сортировки и хэши уходят во временные файлы",
                     [f"Временных файлов {_f(t['temp_files'])}, {_size(t['temp_bytes'])} "
                      f"({_size(per_h)} в час)", f"work_mem = {_size(wm)}"],
                     ["Увеличьте work_mem для запросов, которые пишут временные файлы: точечно — "
                      "SET work_mem в сессии отчёта/ETL, глобально — осторожно (лимит на каждую операцию "
                      "каждого соединения).",
                      "Включите log_temp_files = 0, чтобы увидеть такие запросы в журнале."],
                     ["ALTER SYSTEM SET work_mem = '32MB';", "ALTER SYSTEM SET log_temp_files = 0;",
                      "SELECT pg_reload_conf();"], self.top_sql("temp"))

    def checkpoints(self):
        cp = self.rep["instance"]["checkpoints"]
        req, timed = cp["requested"], cp["timed"]
        if req >= 3 and req > timed:
            mws = self.r.setting_bytes("max_wal_size")
            self.add("warning", "WAL", "Контрольные точки запускаются по объёму WAL, а не по времени",
                     [f"По требованию: {req}, по времени: {timed} ({cp['per_hour']} в час)",
                      f"max_wal_size = {_size(mws)}, WAL ≈ {_f(self.rep['instance']['wal_mb_per_hour'])} МБ/ч"],
                     ["Частые контрольные точки увеличивают запись (full page writes) и нагрузку на диск. "
                      "Увеличьте max_wal_size так, чтобы между контрольными точками проходило checkpoint_timeout.",
                      "checkpoint_completion_target = 0.9 растягивает запись на весь интервал."],
                     ["ALTER SYSTEM SET max_wal_size = '4GB';",
                      "ALTER SYSTEM SET checkpoint_completion_target = 0.9;", "SELECT pg_reload_conf();"])
        bg = self.r.bg
        written = bg["buffers_checkpoint"] + bg["buffers_clean"] + bg["buffers_backend"]
        if written > 10000 and bg["buffers_backend"] > 0.3 * written:
            self.add("info", "Запись", "Обслуживающие процессы сами записывают грязные страницы",
                     [f"{_f(100 * bg['buffers_backend'] / written)}% записей сделали обслуживающие процессы",
                      f"bgwriter остановился по лимиту {_f(bg['maxwritten_clean'])} раз"],
                     ["Фоновый процесс записи не успевает: увеличьте bgwriter_lru_maxpages и уменьшите "
                      "bgwriter_delay, проверьте размер shared_buffers."],
                     ["ALTER SYSTEM SET bgwriter_lru_maxpages = 400;", "ALTER SYSTEM SET bgwriter_delay = '50ms';",
                      "SELECT pg_reload_conf();"])
        if bg["buffers_backend_fsync"]:
            self.add("warning", "Запись", "Обслуживающие процессы выполняют fsync сами",
                     [f"buffers_backend_fsync = {_f(bg['buffers_backend_fsync'])}"],
                     ["Очередь синхронизации checkpointer переполнена — признак перегруженного диска; "
                      "проверьте подсистему хранения и параметры контрольных точек."])

    def locking(self):
        lock = self.share("Lock")
        if lock >= 10:
            ash = sorted(self.rep["sql"].get("ash") or [], key=lambda x: x.get("lock") or 0, reverse=True)
            ids = [x["sql_id"] for x in ash[:3] if x.get("lock")]
            ev = [f"Ожидание блокировок — {_f(lock)}% времени активных сессий"]
            for e in self.rep["events"]:
                if e["wait_class"] == "Lock":
                    ev.append(f"{e['event']}: {_f(e['pct_db_time'])}%")
            self.add("critical" if lock >= 30 else "warning", "Блокировки", "Сессии ждут блокировок", ev[:5],
                     ["transactionid/tuple — конкуренция за одни и те же строки: короче транзакции, обновляйте "
                      "строки в одном порядке, вынесите «горячие» счётчики.",
                      "relation — DDL или LOCK TABLE во время работы приложения.",
                      "Кто кого блокирует — вкладка «Блокировки» в основном окне."], sql_ids=ids)
        if self.r.tot["deadlocks"]:
            self.add("warning", "Блокировки", "Взаимоблокировки (deadlock)",
                     [f"Взаимоблокировок: {self.r.tot['deadlocks']}"],
                     ["Транзакции захватывают строки в разном порядке. Подробности — в журнале сервера "
                      "(log_lock_waits = on покажет и долгие ожидания)."],
                     ["ALTER SYSTEM SET log_lock_waits = on;", "SELECT pg_reload_conf();"])

    def transactions(self):
        inst = self.rep["instance"]
        idle = next((r for r in inst["by_state"] if r["state"] == "idle in transaction"), None)
        if idle and idle["sessions"] and (idle.get("longest_in_state_s") or 0) > 300:
            self.add("warning", "Транзакции", "Сессии простаивают внутри открытых транзакций",
                     [f"«idle in transaction»: {idle['sessions']}, дольше всех {_f(idle['longest_in_state_s'])} с"],
                     ["Открытая транзакция держит блокировки и мешает автоочистке удалять старые версии строк. "
                      "Исправьте приложение (commit/rollback сразу после работы) и ограничьте простой."],
                     ["ALTER SYSTEM SET idle_in_transaction_session_timeout = '10min';",
                      "SELECT pg_reload_conf();"])
        if inst["long_xacts"]:
            x = inst["long_xacts"][0]
            self.add("info", "Транзакции", "Долгие транзакции",
                     [f"pid {x['pid']} ({x['usename']}@{x['datname']}, {x['state']}): {_f(x['xact_s'])} с"]
                     + [f"всего дольше 5 мин: {len(inst['long_xacts'])}"],
                     ["Долгие транзакции не дают очистке (VACUUM) удалять мёртвые строки во всей базе."],
                     [f"SELECT pg_terminate_backend({x['pid']});  -- если транзакция зависла"])
        rb = self.r.tot["xact_rollback"]
        if self.r.txn > 1000 and rb / self.r.txn > 0.05:
            self.add("info", "Приложение", "Много откатов транзакций",
                     [f"Откатов {_f(100 * rb / self.r.txn)}% транзакций"],
                     ["Частые ROLLBACK — ошибки в приложении или пул соединений, откатывающий сессии при "
                      "возврате; проверьте журнал ошибок."])

    def vacuum(self):
        t = self.rep["tables"]
        bad = [r for r in t["dead"] if (r["dead_pct"] or 0) >= 20 and r["dead"] >= 10000]
        if bad:
            self.add("warning", "Автоочистка", "Много «мёртвых» строк в таблицах",
                     [f"{r['table']}: {_f(r['dead'])} мёртвых ({_f(r['dead_pct'])}%), "
                      f"автоочистка: {r['last_autovacuum'] or 'не было'}" for r in bad[:5]],
                     ["Автоочистка не успевает: снизьте autovacuum_vacuum_scale_factor для больших таблиц "
                      "и увеличьте autovacuum_vacuum_cost_limit.",
                      "Проверьте, нет ли долгих транзакций, которые мешают очистке."],
                     [f"ALTER TABLE {bad[0]['table']} SET (autovacuum_vacuum_scale_factor = 0.02);",
                      f"VACUUM (ANALYZE, VERBOSE) {bad[0]['table']};"])
        if (self.r.setting("autovacuum") or "on") == "off":
            self.add("critical", "Автоочистка", "Автоочистка выключена", ["autovacuum = off"],
                     ["Без автоочистки таблицы раздуваются, статистика устаревает, а при приближении к "
                      "переполнению счётчика транзакций база остановится."],
                     ["ALTER SYSTEM SET autovacuum = on;", "SELECT pg_reload_conf();"])
        old = [d for d in self.rep["databases"] if (d.get("xid_age") or 0) > 1_000_000_000]
        if old:
            self.add("critical" if any(d["xid_age"] > 1_500_000_000 for d in old) else "warning",
                     "Автоочистка", "Приближается переполнение счётчика транзакций (XID wraparound)",
                     [f"{d['datname']}: возраст {_f(d['xid_age'])} из ~2 100 000 000" for d in old],
                     ["Выполните VACUUM (FREEZE) в этих базах и выясните, что мешает автоочистке "
                      "(долгие транзакции, неактивные слоты репликации, prepared transactions)."],
                     ["VACUUM (FREEZE, VERBOSE);"])
        stale = [r for r in self.r.tables_delta()
                 if (r.get("mod_since_analyze") or 0) > max(10000, 0.2 * (r["live"] or 0))]
        if stale:
            stale.sort(key=lambda r: r["mod_since_analyze"], reverse=True)
            self.add("info", "Статистика", "Статистика планировщика устарела",
                     [f"{r['table']}: изменено {_f(r['mod_since_analyze'])} строк после ANALYZE" for r in stale[:5]],
                     ["Устаревшая статистика приводит к неудачным планам. Выполните ANALYZE или снизьте "
                      "autovacuum_analyze_scale_factor."],
                     [f"ANALYZE {r['table']};" for r in stale[:3]])

    def scans(self):
        t = self.rep["tables"]
        big = [r for r in t["seq_tup_read"] if r["seq_tup_read"] > 1_000_000 and r["size_mb"] >= 10
               and r["seq_scan"] > 10]
        if big:
            self.add("warning", "Индексы", "Частые последовательные сканирования больших таблиц",
                     [f"{r['table']} ({_f(r['size_mb'])} МБ): {_f(r['seq_scan'])} сканирований, "
                      f"{_f(r['seq_tup_read'])} строк прочитано" for r in big[:5]],
                     ["Найдите запросы к этим таблицам (раздел SQL, кнопка плана) и создайте индексы по "
                      "условиям WHERE/JOIN. CREATE INDEX CONCURRENTLY не блокирует запись."],
                     [f"-- CREATE INDEX CONCURRENTLY ON {big[0]['table']} (<столбцы условия>);"],
                     self.top_sql("reads"))
        unused = [i for i in t["unused_indexes"] if (i.get("size") or 0) >= 10 * MB]
        if unused:
            total = sum(i["size"] for i in unused)
            self.add("info", "Индексы", "Неиспользуемые индексы",
                     [f"{i['schemaname']}.{i['index_name']} на {i['table_name']}: {_size(i['size'])}"
                      for i in unused[:6]] + [f"Всего {_size(total)}"],
                     ["Индексы без сканирований с момента сброса статистики только замедляют запись и занимают "
                      "место. Перед удалением убедитесь, что статистика копилась достаточно долго и индекс "
                      "не используется на репликах."],
                     [f"DROP INDEX CONCURRENTLY {unused[0]['schemaname']}.{unused[0]['index_name']};"])

    def io(self):
        io = self.share("IO")
        if io >= 30:
            ev = [f"Ожидание ввода-вывода — {_f(io)}% времени активных сессий"]
            ev += [f"{e['event']}: {_f(e['pct_db_time'])}%" for e in self.rep["events"] if e["wait_class"] == "IO"][:4]
            adv = ["DataFileRead — чтения с диска мимо кэша: индексы и размер shared_buffers.",
                   "WALWrite/WALSync — запись журнала при фиксации: быстрый диск под WAL, "
                   "synchronous_commit = off для некритичных данных, группировка фиксаций."]
            self.add("warning", "Ввод-вывод", "Сессии ждут диск", ev, adv, sql_ids=self.top_sql("reads"))
        lw = self.share("LWLock")
        if lw >= 15:
            ev = [f"{e['event']}: {_f(e['pct_db_time'])}%" for e in self.rep["events"] if e["wait_class"] == "LWLock"][:4]
            self.add("warning", "Конкуренция", "Конкуренция за внутренние блокировки (LWLock)",
                     [f"LWLock — {_f(lw)}% времени"] + ev,
                     ["BufferMapping/buffer_content — «горячие» страницы и маленький shared_buffers; "
                      "WALWrite/WALInsert — интенсивная запись WAL; lock_manager — слишком много секций/"
                      "соединений: пул соединений."])
        client = self.share("Client")
        if client >= 20:
            self.add("info", "Сеть", "Сессии ждут клиента",
                     [f"Client — {_f(client)}% времени"],
                     ["ClientRead/ClientWrite — приложение медленно забирает результат или держит сессию "
                      "активной между командами: проверьте сеть и размер выборок."])

    def statements(self):
        sql = self.rep["sql"]
        if sql.get("source") != "pg_stat_statements":
            note = sql.get("note") or ""
            preload = "pg_stat_statements" in (self.r.setting("shared_preload_libraries") or "")
            cmds = [] if preload else [
                "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';   -- добавьте к имеющимся",
                "-- перезапустите PostgreSQL, затем:"]
            cmds += ["CREATE EXTENSION pg_stat_statements;"]
            self.add("info", "Мониторинг", "Не подключено pg_stat_statements",
                     [note or "Статистика выполнения запросов недоступна"],
                     ["Без pg_stat_statements раздел SQL строится только по выборкам активных сессий: "
                      "видно, что работало, но нет числа выполнений, времени и чтений по каждому запросу."],
                     cmds)
            return
        top = sql.get("elapsed") or []
        if top and (top[0].get("pct_time") or 0) >= 40 and top[0]["elapsed_s"] > 30:
            s = top[0]
            self.add("warning", "SQL", "Один запрос занимает основную часть времени",
                     [f"{_f(s['pct_time'])}% времени всех запросов, {_f(s['executions'])} выполнений, "
                      f"{_f(s['ela_per_exec_ms'], 2)} мс на выполнение"],
                     ["Оптимизация этого запроса даст наибольший эффект: посмотрите план (кнопка SQL), "
                      "индексы и число строк."], sql_ids=[s["sql_id"]])
        slow = [s for s in (sql.get("mean") or []) if (s.get("ela_per_exec_ms") or 0) > 1000 and s["executions"] >= 5]
        if slow:
            self.add("info", "SQL", "Медленные запросы (больше 1 с на выполнение)",
                     [f"{_f(s['ela_per_exec_ms'] / 1000, 2)} с × {_f(s['executions'])}: {(s['text'] or '')[:80]}"
                      for s in slow[:4]],
                     ["Включите log_min_duration_statement, чтобы видеть такие запросы с параметрами в журнале; "
                      "для разбора — EXPLAIN (ANALYZE, BUFFERS)."],
                     ["ALTER SYSTEM SET log_min_duration_statement = '1s';", "SELECT pg_reload_conf();"],
                     [s["sql_id"] for s in slow[:4]])

    def connections(self):
        inst = self.rep["instance"]
        mx = inst["max_connections"] or 0
        if mx and inst["connections"] > 0.8 * mx:
            self.add("warning", "Соединения", "Число соединений близко к max_connections",
                     [f"{inst['connections']} из {mx}"],
                     ["Используйте пул соединений (PgBouncer, пул приложения) вместо увеличения max_connections: "
                      "каждое соединение — отдельный процесс с собственной памятью."])
        idle = next((r for r in inst["by_state"] if r["state"] == "idle"), None)
        if mx and idle and idle["sessions"] > 0.5 * mx:
            self.add("info", "Соединения", "Много простаивающих соединений",
                     [f"idle: {idle['sessions']} из {mx}"],
                     ["Простаивающие соединения расходуют память и слоты; ограничьте размер пулов приложений."])

    def replication(self):
        for s in self.rep["instance"]["slots"]:
            if not s.get("active") and (s.get("retained_bytes") or 0) > 1 << 30:
                self.add("critical", "Репликация", f"Неактивный слот репликации {s['slot_name']} удерживает WAL",
                         [f"Удержано {_size(s['retained_bytes'])} WAL"],
                         ["Неактивный слот не даёт удалять журналы — диск заполнится. Удалите слот, если "
                          "получатель больше не нужен, или задайте max_slot_wal_keep_size (13+)."],
                         [f"SELECT pg_drop_replication_slot('{s['slot_name']}');"])

    def params(self):
        r = self.r
        if (r.setting("track_io_timing") or "off") == "off":
            self.add("info", "Мониторинг", "track_io_timing выключен", ["Время чтения/записи блоков не измеряется"],
                     ["Включите, чтобы видеть время ввода-вывода по запросам и базам (накладные расходы малы "
                      "на современных системах — проверьте pg_test_timing)."],
                     ["ALTER SYSTEM SET track_io_timing = on;", "SELECT pg_reload_conf();"])
        rpc = r.setting("random_page_cost")
        if rpc and float(rpc) >= 4:
            self.add("info", "Параметры", f"random_page_cost = {rpc} (значение для HDD)", [],
                     ["Для SSD/NVMe задайте 1.1–1.5, иначе планировщик недооценивает индексный доступ."],
                     ["ALTER SYSTEM SET random_page_cost = 1.1;", "SELECT pg_reload_conf();"])
        mwm = r.setting_bytes("maintenance_work_mem")
        if mwm and mwm <= 64 * MB:
            self.add("info", "Параметры", "maintenance_work_mem по умолчанию", [f"maintenance_work_mem = {_size(mwm)}"],
                     ["Больше памяти ускоряет VACUUM, CREATE INDEX и восстановление ограничений: 256 МБ–1 ГБ."],
                     ["ALTER SYSTEM SET maintenance_work_mem = '512MB';", "SELECT pg_reload_conf();"])
        q = r.setting("track_activity_query_size")
        if q and int(q) <= 1024:
            self.add("info", "Мониторинг", "Тексты запросов обрезаются до 1 КБ",
                     [f"track_activity_query_size = {q}"],
                     ["Длинные запросы в pg_stat_activity видны не полностью. Увеличение требует перезапуска."],
                     ["ALTER SYSTEM SET track_activity_query_size = 4096;   -- после перезапуска"])
        if (r.setting("jit") or "off") == "on" and self.share("CPU") > 30:
            self.add("info", "Параметры", "JIT включён", [],
                     ["Для коротких OLTP-запросов JIT часто добавляет время компиляции; проверьте планы "
                      "(строка JIT в EXPLAIN ANALYZE) и при необходимости выключите."],
                     ["ALTER SYSTEM SET jit = off;", "SELECT pg_reload_conf();"])

    def run(self):
        for rule in (self.activity, self.cpu, self.cache, self.temp, self.checkpoints, self.locking,
                     self.transactions, self.vacuum, self.scans, self.io, self.statements, self.connections,
                     self.replication, self.params):
            try:
                rule()
            except Exception as e:  # noqa: BLE001 - one broken rule must not hide the others
                self.add("info", "Анализ", f"Правило {rule.__name__} не выполнено: {e}")
        if not any(x["severity"] in ("critical", "warning") for x in self.out):
            self.add("ok", "Итог", "Существенных проблем производительности не выявлено",
                     [f"AAS {self.h['aas']}, транзакций {_f(self.r.txn)} за {_f(self.h['elapsed_min'])} мин"],
                     ["Сохраните этот отчёт как эталон для сравнения в период проблем."])
        self.out.sort(key=lambda x: SEV_ORDER[x["severity"]])
        return self.out


def advise(rpt, rep):
    return Advisor(rpt, rep).run()
