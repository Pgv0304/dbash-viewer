"""Rule-based tuning advice for an AWR-like report (see awr.py).

advise(report_obj, report_dict) -> list of findings:
  {"severity": "critical"|"warning"|"info"|"ok", "area": str, "title": str,
   "evidence": [str], "advice": [str], "commands": [str], "sql_ids": [str]}
Thresholds are deliberately conservative: a finding needs both a ratio and a meaningful share of DB time.
"""
from __future__ import annotations
import math
import re

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}


def _f(v, nd=1):
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return f"{v:,.{nd}f}".replace(",", " ")


def _at(advice, factor):
    """Advisory row closest to the given size factor."""
    if not advice:
        return None
    return min(advice, key=lambda r: abs((r.get("size_factor") or 0) - factor))


class Advisor:
    def __init__(self, rpt, rep):
        self.r, self.rep = rpt, rep
        self.out = []
        self.db_time = rpt.db_time
        self.ev = {e["event"]: e for e in rep["events"]}
        banner = (rpt.e["instance"].get("banner") or "")
        self.xe = "Express Edition" in banner
        self.version = rpt.e["instance"].get("version") or ""

    def add(self, severity, area, title, evidence=(), advice=(), commands=(), sql_ids=()):
        self.out.append({"severity": severity, "area": area, "title": title, "evidence": list(evidence),
                         "advice": list(advice), "commands": list(commands), "sql_ids": list(sql_ids)})

    def pct(self, event):
        e = self.ev.get(event)
        return (100.0 * e["time_s"] / self.db_time) if e and self.db_time else 0.0

    def events_like(self, pattern):
        rx = re.compile(pattern)
        return [e for e in self.rep["events"] if rx.search(e["event"])]

    def top_sql(self, key, n=3):
        return [s["sql_id"] for s in self.rep["sql"].get(key, [])[:n]]

    # ------------------------------------------------------------------ rules

    def activity(self):
        h = self.rep["header"]
        if self.db_time < 30 or h["aas"] < 0.02:
            self.add("info", "Нагрузка", "Нагрузка в интервале низкая — выводы ограничены",
                     [f"DB time {_f(self.db_time)} с за {_f(h['elapsed_min'])} мин (AAS {h['aas']})"],
                     ["Для анализа выберите снимки, охватывающие период типичной или пиковой нагрузки."])

    def cpu(self):
        h, os_ = self.rep["header"], self.rep["os"]
        cpus = max(1, self.r.cpus)
        cap = self.r.db_cpu / (self.r.elapsed * cpus) * 100
        ev = [f"DB CPU {_f(self.r.db_cpu)} с — {_f(cap)}% мощности {cpus} CPU",
              f"Среднее число активных сессий (AAS) {h['aas']} при {cpus} CPU"]
        if os_.get("busy_pct") is not None:
            ev.append(f"Загрузка CPU сервера (ОС) {_f(os_['busy_pct'])}%")
        overloaded = (os_.get("busy_pct") or 0) > 85 or cap > 85
        if overloaded or cap > 65:
            ids = self.top_sql("cpu")
            self.add("critical" if overloaded else "warning", "CPU",
                     "Нехватка процессорного времени" if overloaded else "Высокая загрузка CPU",
                     ev,
                     ["Оптимизируйте SQL с наибольшим CPU (список ниже): лишние логические чтения обычно "
                      "означают неудачный план — проверьте индексы и статистику.",
                      "Сократите жёсткие разборы и частые входы в систему, если они есть в находках.",
                      "Если нагрузка оправдана — нужно больше CPU" +
                      (" (в Oracle XE экземпляр всё равно использует не больше одного ядра — для роста нужна "
                       "Standard/Enterprise Edition)" if self.xe else "") + "."],
                     sql_ids=ids)
        if h["aas"] > cpus * 1.5 and self.r.db_time > 60:
            self.add("warning", "CPU", "Очередь к ресурсам: активных сессий больше, чем CPU",
                     [f"AAS {h['aas']} при {cpus} CPU"],
                     ["Сессии ждут CPU или ресурсы базы: смотрите главные события ожидания и топ SQL; "
                      "ограничьте параллелизм приложения (пул соединений) или распределите пакетные задания "
                      "по времени."])

    def parsing(self):
        ss, el = self.r.ss, self.r.elapsed
        parses, hard = ss.get("parse count (total)", 0), ss.get("parse count (hard)", 0)
        execs = ss.get("execute count", 0)
        hard_s = hard / el
        soft = 100 * (parses - hard) / parses if parses else 100
        lib = sum(self.pct(e) for e in ("library cache: mutex X", "cursor: pin S wait on X", "latch: shared pool",
                                        "library cache lock", "library cache load lock", "cursor: pin S",
                                        "latch: row cache objects"))
        parse_time = self.r.tm.get("hard parse elapsed time", 0) / 1e6
        parse_pct = 100 * parse_time / self.db_time if self.db_time else 0
        if hard_s > 10 or (parses / el > 1 and soft < 90) or parse_pct > 10:
            literal = self.literal_groups()
            ev = [f"Жёстких разборов {_f(hard_s)}/с, Soft Parse {_f(soft)}%",
                  f"Время жёсткого разбора {_f(parse_time)} с ({_f(parse_pct)}% DB time)"]
            if lib > 1:
                ev.append(f"Ожидания библиотечного кэша/разбора {_f(lib)}% DB time")
            ev += [f"Похожие запросы без связанных переменных: «{t}…» — {n} разных SQL ID" for t, n in literal[:3]]
            sev = "critical" if parse_pct > 20 or hard_s > 100 else "warning"
            self.add(sev, "Разбор SQL", "Много жёстких разборов (литералы вместо связанных переменных)", ev,
                     ["Перепишите запросы приложения со связанными переменными (:p1 вместо констант) — "
                      "это главный и правильный способ.",
                      "Временная мера, пока приложение не исправлено: CURSOR_SHARING=FORCE "
                      "(может менять планы — проверьте на тесте)."],
                     ["ALTER SYSTEM SET cursor_sharing = FORCE SCOPE = BOTH;"])
        if execs and parses / el > 20 and (execs - parses) / execs < 0.5:
            cached = self.r.iparam("session_cached_cursors", 50)
            self.add("info", "Разбор SQL", "Низкий Execute to Parse: курсоры разбираются при каждом выполнении",
                     [f"Execute to Parse {_f(100 * (execs - parses) / execs)}%, разборов {_f(parses / el)}/с",
                      f"session_cached_cursors = {cached}"],
                     ["Держите подготовленные выражения открытыми в приложении (кэш выражений драйвера/пула).",
                      "Увеличьте кэш курсоров сессии (вступит в силу после перезапуска экземпляра)."],
                     [f"ALTER SYSTEM SET session_cached_cursors = {max(200, cached * 2)} SCOPE = SPFILE;"])

    def literal_groups(self):
        groups = {}
        for s in self.rep["sql"].get("parses", []) + self.rep["sql"].get("executions", []) + \
                self.rep["sql"].get("cpu", []):
            t = re.sub(r"\s+", " ", (s.get("text") or "")).strip()
            key = re.sub(r"('[^']*'|\b\d+(\.\d+)?\b)", "?", t)[:70]
            if key:
                groups.setdefault(key, set()).add(s["sql_id"])
        return sorted(((k, len(v)) for k, v in groups.items() if len(v) >= 5), key=lambda x: -x[1])

    def memory(self):
        adv = self.rep["memory"]["advice"]
        sga_t = self.r.iparam("sga_target")
        mem_t = self.r.iparam("memory_target")
        xe_note = (" Ограничение Oracle XE 11g: SGA + PGA вместе не больше 1 ГБ." if self.xe else "")
        # SGA / buffer cache
        sga_adv = adv.get("sga") or []
        cur, big = _at(sga_adv, 1.0), _at(sga_adv, 1.5)
        pr_s = self.r.ss.get("physical reads", 0) / self.r.elapsed
        if cur and big and cur.get("db_time") and big["size_factor"] > 1.2:
            gain = 100 * (cur["db_time"] - big["db_time"]) / cur["db_time"]
            if gain >= 10:
                self.add("warning", "Память", "Увеличение SGA заметно сократит DB time",
                         [f"По советнику SGA: {cur['size_mb']} МБ → {big['size_mb']} МБ сокращает DB time на "
                          f"{_f(gain)}% и физические чтения с {_f(cur.get('reads'), 0)} до {_f(big.get('reads'), 0)}"],
                         ["Увеличьте SGA_TARGET (в пределах SGA_MAX_SIZE и свободной памяти сервера)." + xe_note],
                         [f"ALTER SYSTEM SET sga_target = {big['size_mb']}M SCOPE = BOTH;"])
        cache = adv.get("db_cache") or []
        c1, c2 = _at(cache, 1.0), _at(cache, 2.0)
        hit = next((x["value"] for x in self.rep["efficiency"] if x["name"] == "Buffer Hit %"), None)
        if c1 and c2 and c2["size_factor"] > 1.4 and (c2.get("read_factor") or 1) < 0.8 and pr_s > 20 \
                and not sga_t and not mem_t:
            self.add("warning", "Память", "Мал буферный кэш",
                     [f"Buffer Hit {_f(hit)}%, физических чтений {_f(pr_s)}/с",
                      f"Советник: кэш {c2['size_mb']} МБ сократит чтения до {_f(c2['read_factor'] * 100)}%"],
                     ["Увеличьте DB_CACHE_SIZE." + xe_note],
                     [f"ALTER SYSTEM SET db_cache_size = {c2['size_mb']}M SCOPE = BOTH;"])
        elif hit is not None and hit < 90 and pr_s > 50:
            self.add("info", "Память", "Низкий Buffer Hit %",
                     [f"Buffer Hit {_f(hit)}%, физических чтений {_f(pr_s)}/с"],
                     ["Чаще всего причина — полные сканирования больших таблиц: проверьте SQL по физическим "
                      "чтениям. Увеличение кэша поможет, только если советник показывает выигрыш."],
                     sql_ids=self.top_sql("reads"))
        sp = adv.get("shared_pool") or []
        s1, s2 = _at(sp, 1.0), _at(sp, 1.5)
        if s1 and s2 and s2["size_factor"] > 1.2 and s1.get("load_time_s"):
            saved = (s2["time_saved_s"] or 0) - (s1["time_saved_s"] or 0)
            if saved > 0.1 * s1["load_time_s"] and saved > 60:
                self.add("info", "Память", "Shared pool мал для библиотечного кэша",
                         [f"Советник: shared pool {s2['size_mb']} МБ сэкономит {_f(saved)} с загрузки объектов"],
                         ["Увеличьте SGA_TARGET (shared pool вырастет автоматически) или задайте минимум "
                          "SHARED_POOL_SIZE. Но если причина в литералах — сначала исправьте их." + xe_note],
                         [f"ALTER SYSTEM SET shared_pool_size = {s2['size_mb']}M SCOPE = BOTH;"])
        # PGA
        pga = adv.get("pga") or []
        p1 = _at(pga, 1.0)
        temp = sum(self.pct(e) for e in ("direct path read temp", "direct path write temp"))
        ss = self.r.ss
        sorts_d, sorts_m = ss.get("sorts (disk)", 0), ss.get("sorts (memory)", 0)
        disk_sort_pct = 100 * sorts_d / (sorts_d + sorts_m) if sorts_d + sorts_m else 0
        cur_pga = self.r.iparam("pga_aggregate_target") // 1048576
        if p1 and ((p1.get("overalloc") or 0) > 0 or (p1.get("hit_pct") or 100) < 85 or temp > 5
                   or disk_sort_pct > 1):
            target = next((r for r in pga if r["size_factor"] > 1 and r["size_mb"] > cur_pga
                           and (r.get("overalloc") or 0) == 0 and (r.get("hit_pct") or 0) >= 95), None)
            advisor_gain = (p1.get("overalloc") or 0) > 0 or (p1.get("hit_pct") or 100) < 95
            ev = [f"PGA cache hit по советнику {_f(p1.get('hit_pct'))}%, over-allocation {p1.get('overalloc')}, "
                  f"pga_aggregate_target {cur_pga} МБ",
                  f"Сортировок на диске {_f(disk_sort_pct, 2)}%, временный ввод-вывод {_f(temp)}% DB time"]
            if advisor_gain and target:
                self.add("warning", "Память",
                         "Не хватает PGA: сортировки и хеш-соединения уходят во временное табличное пространство",
                         ev, [f"Увеличьте PGA_AGGREGATE_TARGET до {target['size_mb']} МБ." + xe_note],
                         [f"ALTER SYSTEM SET pga_aggregate_target = {target['size_mb']}M SCOPE = BOTH;"],
                         self.top_sql("io"))
            else:
                self.add("warning" if temp > 10 else "info", "Ввод-вывод",
                         "Сортировки и хеш-соединения выполняются на диске (TEMP)", ev,
                         ["Советник PGA не показывает выигрыша от увеличения памяти — причина в отдельных "
                          "операциях: очень большие сортировки/хеш-соединения или сессии с ручными настройками "
                          "(WORKAREA_SIZE_POLICY=MANUAL, малый SORT_AREA_SIZE). Найдите их по SQL с наибольшим "
                          "временем ввода-вывода и сократите объём сортируемых данных (условия, индексы, "
                          "отказ от лишнего ORDER BY/DISTINCT)."],
                         sql_ids=self.top_sql("io"))

    def resource_manager(self):
        p = self.pct("resmgr:cpu quantum")
        if p > 2:
            plan = self.r.param("resource_manager_plan") or "—"
            self.add("info", "CPU", "Resource Manager ограничивает использование CPU",
                     [f"resmgr:cpu quantum {_f(p)}% DB time, resource_manager_plan = {plan}"],
                     ["Сессии ждут, пока Resource Manager выделит им квант CPU. Часто это план окна "
                      "обслуживания (DEFAULT_MAINTENANCE_PLAN) в ночное время — тогда это ожидаемо. "
                      "Если ограничение мешает рабочей нагрузке — пересмотрите план или время окон "
                      "обслуживания (DBMS_SCHEDULER, окна *_WINDOW)."])

    def redo(self):
        ls = self.rep["log_switches"]
        logs = ls.get("logs") or []
        size_mb = max((l["bytes"] for l in logs), default=0) / 1048576
        sw = sum(self.pct(e) for e in ("log file switch (checkpoint incomplete)", "log file switch completion",
                                       "log file switch (archiving needed)", "log file switch (private strand flush incomplete)"))
        if ls["per_hour"] > 6 or sw > 1:
            redo_h = self.r.ss.get("redo size", 0) / self.r.elapsed * 3600 / 1048576
            new = max(200, int(math.ceil(redo_h / 4 / 100.0)) * 100)
            groups = max(3, len(logs))
            cmds = [f"ALTER DATABASE ADD LOGFILE GROUP {10 + i} SIZE {new}M;" for i in range(groups)]
            cmds += ["-- дождитесь, пока старые группы станут INACTIVE (ALTER SYSTEM SWITCH LOGFILE; "
                     "ALTER SYSTEM CHECKPOINT;), затем удалите их:"]
            cmds += [f"ALTER DATABASE DROP LOGFILE GROUP {l['group#']};" for l in logs]
            self.add("warning" if sw > 1 or ls["per_hour"] > 12 else "info", "Redo",
                     "Слишком частые переключения журналов (малые redo-логи)",
                     [f"Переключений {_f(ls['per_hour'])} в час, размер журналов {_f(size_mb, 0)} МБ, "
                      f"групп {len(logs)}",
                      f"Ожидания переключения журналов {_f(sw)}% DB time",
                      f"Объём redo {_f(redo_h)} МБ/ч"],
                     [f"Увеличьте журналы до ~{new} МБ (цель — 2–4 переключения в час) и держите не меньше 3 групп.",
                      f"Проверьте место на диске: новые журналы займут {groups} × {new} МБ = "
                      f"{_f(groups * new / 1024, 1)} ГБ (старые освободят {_f(size_mb * len(logs), 0)} МБ). "
                      "Если места мало — выберите меньший размер: даже 2–3-кратное увеличение заметно "
                      "сократит число переключений."],
                     cmds)
        lfs = self.ev.get("log file sync")
        if lfs and self.pct("log file sync") > 5:
            commits = self.r.ss.get("user commits", 0) / self.r.elapsed
            lfpw = self.ev.get("log file parallel write")
            ev = [f"log file sync {_f(self.pct('log file sync'))}% DB time, среднее {_f(lfs['avg_ms'], 2)} мс",
                  f"Фиксаций {_f(commits)}/с"]
            if lfpw:
                ev.append(f"Запись LGWR (log file parallel write) в среднем {_f(lfpw['avg_ms'], 2)} мс")
            adv = []
            if commits > 20:
                adv.append("Приложение фиксирует слишком часто: объединяйте изменения в пакеты (commit раз в N строк).")
            if lfpw and (lfpw["avg_ms"] or 0) > 10:
                adv.append("Запись redo медленная: вынесите журналы на быстрый диск (SSD), отдельно от данных.")
            adv.append("Для некритичных к потере данных операций допустимо COMMIT WRITE BATCH NOWAIT "
                       "(риск потерять последние транзакции при сбое).")
            self.add("warning", "Redo", "Ожидание фиксации транзакций (log file sync)", ev, adv)
        if self.pct("log buffer space") > 1:
            lb = self.r.iparam("log_buffer")
            self.add("info", "Redo", "Не хватает буфера журнала",
                     [f"log buffer space {_f(self.pct('log buffer space'))}% DB time, log_buffer {_f(lb / 1048576)} МБ"],
                     ["Увеличьте LOG_BUFFER (после перезапуска) и проверьте скорость диска журналов."],
                     [f"ALTER SYSTEM SET log_buffer = {max(16, int(lb / 1048576) * 2)}M SCOPE = SPFILE;"])

    def locking(self):
        p = self.pct("enq: TX - row lock contention")
        if p > 5:
            segs = self.rep["segments"].get("row lock waits", [])[:3]
            ev = [f"Ожидание блокировок строк {_f(p)}% DB time "
                  f"({_f(self.ev['enq: TX - row lock contention']['time_s'])} с)"]
            ev += [f"Сегмент {s['owner']}.{s['object']}: {s['value']} ожиданий блокировки" for s in segs]
            self.add("critical" if p > 25 else "warning", "Блокировки",
                     "Сессии ждут строки, заблокированные другими транзакциями", ev,
                     ["Это проблема логики приложения, а не настроек базы: сократите время транзакций "
                      "(фиксируйте сразу после изменения, не держите транзакцию во время ожидания пользователя), "
                      "обновляйте строки в одном порядке, избегайте «горячих» строк-счётчиков (используйте "
                      "последовательности).",
                      "Кто кого блокирует — вкладка «Блокировки» на главном экране."],
                     sql_ids=self.top_sql("application"))
        itl = self.pct("enq: TX - allocate ITL entry") + self.pct("enq: TX - index contention")
        if itl > 2:
            segs = (self.rep["segments"].get("ITL waits") or [])[:3]
            self.add("info", "Блокировки", "Конкуренция за слоты транзакций (ITL) / разделение индексных блоков",
                     [f"{_f(itl)}% DB time"] + [f"{s['owner']}.{s['object']}: {s['value']}" for s in segs],
                     ["Для таблиц с интенсивными параллельными изменениями увеличьте INITRANS (и PCTFREE); "
                      "для индексов по возрастающему ключу рассмотрите reverse key или hash-партиционирование."],
                     [f"ALTER TABLE {s['owner']}.{s['object']} INITRANS 10;" for s in segs if s["type"] == "TABLE"])
        if self.pct("enq: TM - contention") > 2:
            self.add("warning", "Блокировки", "Блокировки таблиц (enq: TM) — вероятно, внешние ключи без индексов",
                     [f"enq: TM - contention {_f(self.pct('enq: TM - contention'))}% DB time"],
                     ["Создайте индексы на столбцах внешних ключей дочерних таблиц: без них удаление/изменение "
                      "родительской строки блокирует всю дочернюю таблицу."])

    def hot_blocks(self):
        p = self.pct("buffer busy waits") + self.pct("read by other session")
        cbc = self.pct("latch: cache buffers chains")
        if p > 5 or cbc > 3:
            segs = (self.rep["segments"].get("buffer busy waits") or [])[:3]
            self.add("warning", "Конкуренция", "Горячие блоки: сессии ждут одни и те же блоки буферного кэша",
                     [f"buffer busy waits + read by other session {_f(p)}% DB time",
                      f"latch: cache buffers chains {_f(cbc)}% DB time"] +
                     [f"{s['owner']}.{s['object']}: {s['value']}" for s in segs],
                     ["Сократите логические чтения проблемных SQL (часто это неселективные индексы или вложенные "
                      "циклы по большому набору).",
                      "Для вставок в одну таблицу из многих сессий — ASSM-табличное пространство, "
                      "больше freelists/INITRANS, партиционирование."],
                     sql_ids=self.top_sql("gets"))

    def io(self):
        seq, sca = self.ev.get("db file sequential read"), self.ev.get("db file scattered read")
        dpr = self.pct("direct path read")
        if seq and (seq["avg_ms"] or 0) > 15 and self.pct("db file sequential read") > 10:
            self.add("warning", "Ввод-вывод", "Медленное одноблочное чтение с диска",
                     [f"db file sequential read: в среднем {_f(seq['avg_ms'], 2)} мс "
                      f"({_f(self.pct('db file sequential read'))}% DB time); норма для SSD < 2 мс, для HDD < 10 мс"],
                     ["Проверьте подсистему хранения (iostat, нагрузка соседей по диску/VPS).",
                      "Сократите число чтений: SQL по физическим чтениям, индексы с плохим clustering factor."],
                     sql_ids=self.top_sql("reads"))
        full = self.pct("db file scattered read") + dpr
        if full > 20:
            self.add("warning", "Ввод-вывод", "Много полных сканирований таблиц",
                     [f"db file scattered read + direct path read {_f(full)}% DB time"] +
                     ([f"Среднее многоблочное чтение {_f(sca['avg_ms'], 2)} мс"] if sca else []),
                     ["Проверьте планы SQL по физическим чтениям: не хватает индексов или устарела статистика. "
                      "Полное сканирование оправдано, только если запрос читает большую часть таблицы."],
                     sql_ids=self.top_sql("reads"))
        for t in self.rep["io"]:
            if t["reads"] > 1000 and (t["avg_read_ms"] or 0) > 20:
                self.add("info", "Ввод-вывод", f"Медленные чтения в табличном пространстве {t['tablespace']}",
                         [f"{_f(t['reads'], 0)} чтений, в среднем {_f(t['avg_read_ms'], 2)} мс"],
                         ["Проверьте диск, на котором лежат файлы этого табличного пространства."])
        if self.pct("free buffer waits") > 2 or self.pct("write complete waits") > 2:
            self.add("warning", "Ввод-вывод", "DBWR не успевает записывать изменённые блоки",
                     [f"free buffer waits {_f(self.pct('free buffer waits'))}%, write complete waits "
                      f"{_f(self.pct('write complete waits'))}% DB time"],
                     ["Ускорьте запись данных (диск, асинхронный ввод-вывод FILESYSTEMIO_OPTIONS=SETALL), "
                      "при необходимости увеличьте буферный кэш."],
                     ["ALTER SYSTEM SET filesystemio_options = SETALL SCOPE = SPFILE;"])

    def network(self):
        p = self.pct("SQL*Net more data to client") + self.pct("SQL*Net more data from client")
        if p > 5:
            self.add("info", "Сеть", "Большие объёмы данных по сети к клиенту",
                     [f"SQL*Net more data {_f(p)}% DB time"],
                     ["Увеличьте размер выборки (arraysize/fetch size) в драйвере, не выбирайте лишние столбцы "
                      "и строки, проверьте пропускную способность сети до клиентов."])

    def sql_concentration(self):
        for s in self.rep["sql"].get("elapsed", [])[:3]:
            if (s.get("pct_db_time") or 0) > 25 and s["elapsed_s"] > 30:
                ev = [f"{s['sql_id']}: {_f(s['pct_db_time'])}% DB time, {_f(s['executions'], 0)} выполнений, "
                      f"{_f(s['ela_per_exec_ms'])} мс и {_f(s['gets_per_exec'])} логических чтений на выполнение"]
                if s.get("text"):
                    ev.append(s["text"][:150])
                self.add("warning", "SQL", f"Один запрос занимает {_f(s['pct_db_time'])}% времени базы", ev,
                         ["Начните настройку с этого запроса: план выполнения (кнопка SQL ID), индексы, "
                          "актуальность статистики. Если запрос выполняется очень часто — уменьшите число "
                          "вызовов (кэширование на стороне приложения)."], sql_ids=[s["sql_id"]])
        heavy = [s for s in self.rep["sql"].get("gets", []) if (s.get("gets_per_exec") or 0) > 100_000][:3]
        if heavy:
            self.add("info", "SQL", "Запросы с очень большим числом логических чтений на выполнение",
                     [f"{s['sql_id']}: {_f(s['gets_per_exec'])} чтений/выполнение" for s in heavy],
                     ["Признак неэффективного плана: проверьте селективность условий и индексы."],
                     sql_ids=[s["sql_id"] for s in heavy])
        multi = [s for s in self.rep["sql"].get("elapsed", []) if (s.get("versions") or 0) > 50][:3]
        if multi:
            self.add("info", "SQL", "Много дочерних курсоров (high version count)",
                     [f"{s['sql_id']}: {s['versions']} версий" for s in multi],
                     ["Проверьте V$SQL_SHARED_CURSOR: частые причины — разные типы/длины связанных переменных, "
                      "разные параметры оптимизатора у сессий."], sql_ids=[s["sql_id"] for s in multi])

    def limits(self):
        for l in self.rep["limits"]:
            try:
                limit = int(l["limit_value"])
            except (TypeError, ValueError):
                continue
            if limit and l["max_used"] / limit > 0.8:
                name = l["resource_name"]
                cmd = [f"ALTER SYSTEM SET {name} = {int(limit * 1.5)} SCOPE = SPFILE;"] \
                    if name in ("processes", "sessions") else []
                self.add("warning", "Ресурсы", f"Почти исчерпан лимит {name}",
                         [f"Максимум за время работы {l['max_used']} из {limit}, сейчас {l['cur']}"],
                         [f"Увеличьте {name.upper()} (вступит в силу после перезапуска) или ограничьте размер "
                          "пулов соединений приложений."], cmd)
        logons = self.r.ss.get("logons cumulative", 0) / self.r.elapsed
        if logons > 1:
            self.add("info", "Ресурсы", "Частые подключения к базе",
                     [f"{_f(logons, 2)} входов в секунду"],
                     ["Используйте пул соединений в приложении: каждое подключение — это новый серверный процесс "
                      "и заметная нагрузка на CPU."])

    def undo(self):
        u = self.rep["undo"]
        ret = self.r.iparam("undo_retention", 900)
        if u["snapshot_too_old"] > 0:
            need = max(ret, int(u["max_query_s"] * 1.2))
            self.add("critical", "Undo", "Ошибки ORA-01555 (snapshot too old)",
                     [f"{u['snapshot_too_old']} ошибок, самый долгий запрос {_f(u['max_query_s'], 0)} с, "
                      f"undo_retention {ret} с"],
                     ["Увеличьте UNDO_RETENTION и обеспечьте место в табличном пространстве отмены "
                      "(автоувеличение файла)."],
                     [f"ALTER SYSTEM SET undo_retention = {need} SCOPE = BOTH;"])
        if u["no_space"] > 0:
            self.add("critical", "Undo", "Не хватает места в табличном пространстве отмены",
                     [f"{u['no_space']} ошибок нехватки места"],
                     ["Добавьте файл или включите автоувеличение для UNDO-табличного пространства."])

    def statistics(self):
        st = self.rep["stale_stats"]
        if st:
            total = sum((r["stale"] or 0) + (r["never"] or 0) for r in st)
            self.add("warning" if total > 10 else "info", "Статистика оптимизатора",
                     "Устаревшая или отсутствующая статистика таблиц",
                     [f"{r['owner']}: устаревшая у {r['stale']}, нет статистики у {r['never']} таблиц" for r in st[:5]],
                     ["Без актуальной статистики оптимизатор выбирает плохие планы. Соберите статистику и "
                      "убедитесь, что включено автоматическое задание сбора."],
                     [f"EXEC DBMS_STATS.GATHER_SCHEMA_STATS('{r['owner']}', options => 'GATHER AUTO');"
                      for r in st[:5]])

    def params(self):
        if (self.r.param("statistics_level") or "").upper() == "BASIC":
            self.add("warning", "Параметры", "STATISTICS_LEVEL=BASIC отключает советники и сбор статистики",
                     [], ["Верните значение TYPICAL."], ["ALTER SYSTEM SET statistics_level = TYPICAL SCOPE = BOTH;"])
        if (self.r.param("timed_statistics") or "").upper() == "FALSE":
            self.add("warning", "Параметры", "TIMED_STATISTICS=FALSE: нет времени ожиданий", [],
                     ["Включите сбор времени."], ["ALTER SYSTEM SET timed_statistics = TRUE SCOPE = BOTH;"])
        oica = self.r.iparam("optimizer_index_cost_adj", 100)
        if oica != 100:
            self.add("info", "Параметры", "Изменён OPTIMIZER_INDEX_COST_ADJ",
                     [f"optimizer_index_cost_adj = {oica}"],
                     ["Этот параметр искажает расчёт стоимости планов во всей базе; обычно лучше вернуть 100 и "
                      "собрать системную статистику."])
        if (self.r.param("cursor_sharing") or "EXACT").upper() != "EXACT":
            self.add("info", "Параметры", f"CURSOR_SHARING = {self.r.param('cursor_sharing')}", [],
                     ["Принудительная замена литералов — временная мера; при исправленном приложении "
                      "верните EXACT."])
        rb = self.r.ss.get("user rollbacks", 0)
        if self.r.txn > 100 and rb / self.r.txn > 0.1:
            self.add("info", "Приложение", "Много откатов транзакций",
                     [f"Откатов {_f(100 * rb / self.r.txn)}% транзакций"],
                     ["Частые ROLLBACK — признак ошибок в приложении или пулов соединений, которые откатывают "
                      "при возврате сессии; проверьте журнал ошибок приложения."])

    def run(self):
        for rule in (self.activity, self.cpu, self.resource_manager, self.parsing, self.memory, self.redo, self.locking,
                     self.hot_blocks, self.io, self.network, self.sql_concentration, self.limits, self.undo,
                     self.statistics, self.params):
            try:
                rule()
            except Exception as e:  # noqa: BLE001 - one broken rule must not hide the others
                self.add("info", "Анализ", f"Правило {rule.__name__} не выполнено: {e}")
        if not any(x["severity"] in ("critical", "warning") for x in self.out):
            self.add("ok", "Итог", "Существенных проблем производительности не выявлено",
                     [f"DB time {_f(self.db_time)} с, AAS {self.rep['header']['aas']}"],
                     ["Сохраните этот отчёт как эталон для сравнения в период проблем."])
        self.out.sort(key=lambda x: SEV_ORDER[x["severity"]])
        return self.out


def advise(rpt, rep):
    return Advisor(rpt, rep).run()
