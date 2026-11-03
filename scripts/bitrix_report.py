#!/usr/bin/env python3
"""Воронки из Битрикс24 через входящий вебхук — вместо скриншотов канбана.

Вебхук берётся из переменной окружения BITRIX_WEBHOOK_URL
(вида https://<портал>.bitrix24.ru/rest/<id>/<код>/). В репозитории его нет
и быть не должно: это ключ доступа к CRM. Скрипт только читает данные.

Запуск:
    python3 scripts/bitrix_report.py sources 2026-10
        какие источники сделок есть за месяц и сколько сделок по каждому
    python3 scripts/bitrix_report.py funnel 2026-10 --source "2|AVITO"
        раскладка по стадиям, категориям квалификации и MEET для источника
    python3 scripts/bitrix_report.py funnel 2026-10 --source WEBFORM --csv /path/deals.csv
        то же + выгрузка сделок в CSV для ручного разбора

Период по умолчанию фильтруется по полю BEGINDATE («Дата начала») — так
снимались все месяцы с мая 2026. Для сверки можно указать --date-field DATE_CREATE.

Категории и MEET — по таксономии раздела 5.1 ANALYTICS_GUIDE.md. Стадии
сопоставляются по названию; всё, что не удалось сопоставить, выводится
отдельной строкой «НЕ РАЗНЕСЕНО» — такие стадии надо дописать в STAGE_RULES.
WON отсюда не берётся: колонка «Сделка успешна» систематически занижает,
источник WON — лист «по месяцам» таблицы «зп».
"""
import argparse, calendar, collections, csv, json, os, sys, time, urllib.error, urllib.request

# (префикс нормализованного названия стадии, категория, входит ли в MEET).
# Порядок важен: «договориться…» проверяется раньше «договор счёт».
STAGE_RULES = [
    ("новые",            "В работе, пока непонятно", False),
    ("вопрос",           "В работе, пока непонятно", False),
    ("созвон",           "В работе, пока непонятно", False),
    ("раскачив",         "В работе, пока непонятно", False),
    ("договориться",     "В работе, пока непонятно", False),
    ("в работе",         "В работе, пока непонятно", False),
    ("на презентац",     "В работе, пока непонятно", True),
    ("надо подумать",    "Квал-лиды", True),
    ("морос",            "Квал-лиды", True),
    ("договор",          "Квал-лиды", True),
    ("сделка провал",    "Квал-лиды", True),
    ("сделка успеш",     "Квал-лиды", True),
    ("бомжик",           "Дорого", True),
    ("отвалил",          "Глухой — не вышел на связь", False),
    ("сотру",            "Спам", False),  # в Битриксе встречается «сотружничество»
    ("2025",             "Не учитывается", False),
]
CATEGORY_ORDER = ["В работе, пока непонятно", "Квал-лиды", "Дорого",
                  "Глухой — не вышел на связь", "Спам", "Не учитывается"]
DEAL_FIELDS = ["ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "SOURCE_ID", "SOURCE_DESCRIPTION",
               "BEGINDATE", "DATE_CREATE", "OPPORTUNITY", "ASSIGNED_BY_ID",
               "ORIGINATOR_ID", "ORIGIN_ID", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN"]
# латинские буквы-двойники → кириллица (в названиях стадий бывают опечатки раскладки)
LOOKALIKE = str.maketrans("aceopxykmthb", "асеорхукмтнв")


def norm(name):
    return " ".join((name or "").lower().replace("ё", "е").translate(LOOKALIKE).split())


def classify(stage_name):
    n = norm(stage_name)
    for prefix, category, meet in STAGE_RULES:
        if n.startswith(prefix):
            return category, meet
    return None, False


def webhook():
    url = os.environ.get("BITRIX_WEBHOOK_URL", "").strip()
    if not url:
        sys.exit("Нет переменной окружения BITRIX_WEBHOOK_URL. Добавьте вебхук в настройки "
                 "окружения сессии (не в чат и не в репозиторий).")
    return url if url.endswith("/") else url + "/"


def call(method, params, _base=[None]):
    """Один вызов REST. Адрес вебхука в сообщения об ошибках не попадает."""
    if _base[0] is None:
        _base[0] = webhook()
    req = urllib.request.Request(_base[0] + method + ".json",
                                 data=json.dumps(params).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 503 and attempt < 3:  # превышен лимит запросов — подождать
                time.sleep(2 ** attempt)
                continue
            try:
                err = json.loads(body)
                msg = f"{err.get('error')}: {err.get('error_description')}"
            except ValueError:
                msg = body[:200]
            sys.exit(f"Битрикс вернул {e.code} на {method} — {msg}")
        except urllib.error.URLError as e:
            sys.exit(f"Битрикс недоступен ({method}): {e.reason}")
    if "error" in data:
        sys.exit(f"Битрикс: {data['error']} — {data.get('error_description')}")
    return data


def call_all(method, params):
    """Постраничная выгрузка списка (Битрикс отдаёт по 50 записей)."""
    out, start = [], 0
    while True:
        data = call(method, dict(params, start=start))
        res = data.get("result", [])
        out.extend(res if isinstance(res, list) else res.get("items", []))
        if "next" not in data:
            return out
        start = data["next"]
        time.sleep(0.4)  # лимит Битрикса — около 2 запросов в секунду


def month_range(ym):
    y, m = (int(x) for x in ym.split("-"))
    return f"{ym}-01", f"{ym}-{calendar.monthrange(y, m)[1]:02d}"


def load_dictionaries():
    statuses = call_all("crm.status.list", {"order": {"SORT": "ASC"}})
    sources = {s["STATUS_ID"]: s["NAME"] for s in statuses if s.get("ENTITY_ID") == "SOURCE"}
    stages, stage_sort = {}, {}
    for s in statuses:
        if str(s.get("ENTITY_ID", "")).startswith("DEAL_STAGE"):
            stages[s["STATUS_ID"]] = s["NAME"]
            stage_sort[s["STATUS_ID"]] = int(s.get("SORT") or 0)
    pipelines = {"0": "Общая"}
    try:
        cats = call("crm.category.list", {"entityTypeId": 2}).get("result", {}).get("categories", [])
        pipelines.update({str(c["id"]): c["name"] for c in cats})
    except SystemExit:
        pass  # на старых порталах метода нет — останутся id
    return sources, stages, stage_sort, pipelines


def load_deals(d1, d2, date_field, source=None):
    flt = {f">={date_field}": d1, f"<={date_field}": d2}
    if source:
        flt["SOURCE_ID"] = source
    return call_all("crm.deal.list", {"filter": flt, "select": DEAL_FIELDS, "order": {"ID": "ASC"}})


def pct(part, whole):
    return f"{part / whole * 100:.1f}%".replace(".", ",") if whole else "—"


def cmd_sources(args):
    d1, d2 = month_range(args.month)
    sources, stages, _, pipelines = load_dictionaries()
    deals = load_deals(d1, d2, args.date_field)
    print(f"Сделки за {d1} — {d2} по полю {args.date_field}: всего {len(deals)}\n")
    by_src = collections.Counter(d.get("SOURCE_ID") or "" for d in deals)
    print(f"{'SOURCE_ID':<22}{'название источника':<38}{'сделок':>7}")
    for sid, n in by_src.most_common():
        print(f"{sid or '(пусто)':<22}{sources.get(sid, '?')[:36]:<38}{n:>7}")
    # подсказки для разнесения по авитологам: чем отличаются сделки одного источника
    print("\nЧастые описания источника (SOURCE_DESCRIPTION) — по ним можно развести авитологов:")
    for sid, _ in by_src.most_common():
        descr = collections.Counter((d.get("SOURCE_DESCRIPTION") or "").strip()[:60]
                                    for d in deals if (d.get("SOURCE_ID") or "") == sid)
        top = [(k, v) for k, v in descr.most_common(6) if k]
        if top:
            print(f"  {sid}:")
            for k, v in top:
                print(f"      {v:>4} × {k}")
    by_pipe = collections.Counter(str(d.get("CATEGORY_ID") or "0") for d in deals)
    print("\nВоронки (направления сделок):")
    for cid, n in by_pipe.most_common():
        print(f"  {cid:>3}  {pipelines.get(cid, '?'):<30}{n:>6}")


def cmd_funnel(args):
    d1, d2 = month_range(args.month)
    sources, stages, stage_sort, pipelines = load_dictionaries()
    deals = load_deals(d1, d2, args.date_field, args.source)
    title = sources.get(args.source, "?") if args.source else "все источники"
    print(f"Источник: {title} ({args.source or '*'}) · {d1} — {d2} · поле {args.date_field}\n")
    if not deals:
        print("Сделок нет.")
        return
    total = len(deals)
    by_stage = collections.Counter(d.get("STAGE_ID") for d in deals)
    cats, meet, unmapped, won_col = collections.Counter(), 0, collections.Counter(), 0
    print(f"{'Стадия':<36}{'Воронка':<16}{'Сделок':>7}")
    for sid in sorted(by_stage, key=lambda x: (stage_sort.get(x, 0), x)):
        name = stages.get(sid, sid)
        cat, is_meet = classify(name)
        n = by_stage[sid]
        pipe = pipelines.get(str(sid).split(":")[0].lstrip("C") if ":" in str(sid) else "0", "")
        print(f"{name[:35]:<36}{pipe[:15]:<16}{n:>7}")
        if cat:
            cats[cat] += n
        else:
            unmapped[name] += n
        meet += n if is_meet else 0
        if norm(name).startswith("сделка успеш"):
            won_col += n
    print(f"{'ИТОГО (TOTAL)':<52}{total:>7}\n")
    print("Квалификация (раздел 5.1 гайда):")
    for c in CATEGORY_ORDER:
        print(f"  {c:<32}{cats[c]:>5}   {pct(cats[c], total):>6}")
    for name, n in unmapped.items():
        print(f"  НЕ РАЗНЕСЕНО: {name:<18}{n:>5}   {pct(n, total):>6}   ← дописать в STAGE_RULES")
    print(f"\n  MEET (провёл встречу)           {meet:>5}   {pct(meet, total):>6}")
    print(f"  «Сделка успешна» (колонка)      {won_col:>5}            WON берётся из листа «по месяцам», не отсюда")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(DEAL_FIELDS + ["STAGE_NAME", "SOURCE_NAME"])
            for d in deals:
                w.writerow([d.get(k, "") for k in DEAL_FIELDS] +
                           [stages.get(d.get("STAGE_ID"), ""), sources.get(d.get("SOURCE_ID"), "")])
        print(f"\nСделки выгружены: {args.csv}")


def main():
    ap = argparse.ArgumentParser(description="Воронки Битрикс24 через вебхук (только чтение)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sources", "funnel"):
        p = sub.add_parser(name)
        p.add_argument("month", help="ГГГГ-ММ, например 2026-10")
        p.add_argument("--date-field", default="BEGINDATE", choices=["BEGINDATE", "DATE_CREATE"])
        if name == "funnel":
            p.add_argument("--source", help="SOURCE_ID из команды sources; без него — все сделки")
            p.add_argument("--csv", help="куда выгрузить сделки в CSV")
    args = ap.parse_args()
    {"sources": cmd_sources, "funnel": cmd_funnel}[args.cmd](args)


if __name__ == "__main__":
    main()
