#!/usr/bin/env python3
"""Сбор данных Яндекс.Директа и Яндекс.Метрики за месяц.

Используется при сборке дашборда `analytics/yandex-<month>.html`.
Токены берутся из переменных окружения (в репозитории их нет):
    YANDEX_DIRECT_TOKEN   — OAuth-токен Яндекс.Директа
    YANDEX_METRIKA_TOKEN  — OAuth-токен Яндекс.Метрики

Запуск:
    python3 scripts/yandex_report.py 2026-10
    python3 scripts/yandex_report.py 2026-09 --counter 111834033

Печатает: расход/показы/клики/CTR/цену клика за месяц, разбивку по неделям
и по кампаниям, визиты и достижения целей по рекламному трафику.
Все цифры — ровно те, что идут в дашборд.
"""
import argparse, calendar, collections, csv, io, json, os, sys, time, urllib.parse, urllib.request

DIRECT = "https://api.direct.yandex.com/json/v5/reports"
METRIKA = "https://api-metrika.yandex.net"
DEFAULT_COUNTER = 111834033
# Цели счётчика PLATFORMANCE: id -> человекочитаемое имя
GOALS = {
    600454388: "Отправка заявки (URL /thank-you)",
    665276568: "Клик по кнопке заявки",
    665276569: "Форма заявки на экране",
    665276571: "Начал заполнять форму",
    665276573: "Форма отправлена (событие Битрикс24)",
    608624675: "Клик по телефону",
}


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Нет переменной окружения {name}. Добавьте токен в настройки окружения сессии.")
    return v


def month_range(ym):
    year, mon = (int(x) for x in ym.split("-"))
    return f"{ym}-01", f"{ym}-{calendar.monthrange(year, mon)[1]:02d}"


def direct_report(d1, d2, retries=20):
    """Отчёт Директа готовится асинхронно: 201/202 — ещё не готов, ждём."""
    body = json.dumps({"params": {
        "SelectionCriteria": {"DateFrom": d1, "DateTo": d2},
        "FieldNames": ["Date", "CampaignName", "Impressions", "Clicks", "Cost"],
        "ReportName": f"pf_{d1}_{d2}_{int(time.time())}",
        "ReportType": "CUSTOM_REPORT", "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV", "IncludeVAT": "YES",
    }}).encode()
    headers = {
        "Authorization": "Bearer " + env("YANDEX_DIRECT_TOKEN"),
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
        "processingMode": "auto", "returnMoneyInMicros": "false",
        "skipReportHeader": "true", "skipReportSummary": "true",
    }
    for attempt in range(retries):
        req = urllib.request.Request(DIRECT, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                if r.status == 200:
                    return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            sys.exit(f"Директ вернул {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        print(f"  отчёт готовится, попытка {attempt + 1}…", file=sys.stderr)
        time.sleep(5)
    sys.exit("Директ не отдал отчёт за отведённое время — повторите запуск.")


def metrika(path, params):
    url = METRIKA + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "OAuth " + env("YANDEX_METRIKA_TOKEN")})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Метрика вернула {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")


def ru(v, digits=1):
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def block(title, impressions, clicks, cost):
    ctr = clicks / impressions * 100 if impressions else 0
    cpc = cost / clicks if clicks else 0
    print(f"{title:<40} показы {impressions:>7}  клики {clicks:>5}  "
          f"CTR {ru(ctr, 2):>5}%  расход {ru(cost):>11} ₽  цена клика {ru(cpc):>7} ₽")


def main():
    ap = argparse.ArgumentParser(description="Данные Яндекс.Директа и Метрики за месяц")
    ap.add_argument("month", help="месяц в формате ГГГГ-ММ, например 2026-10")
    ap.add_argument("--counter", type=int, default=DEFAULT_COUNTER, help="номер счётчика Метрики")
    args = ap.parse_args()
    d1, d2 = month_range(args.month)
    print(f"Период: {d1} — {d2}\n")

    rows = list(csv.DictReader(io.StringIO(direct_report(d1, d2)), delimiter="\t"))
    if not rows:
        print("Директ: расходов за период нет.")
        return

    def agg(rs):
        return (sum(int(r["Impressions"]) for r in rs),
                sum(int(r["Clicks"]) for r in rs),
                sum(float(r["Cost"]) for r in rs))

    print("=== ЯНДЕКС.ДИРЕКТ ===")
    total = agg(rows)
    block("ИТОГО за месяц", *total)

    print("\n--- по неделям ---")
    weeks = collections.defaultdict(list)
    for r in rows:
        weeks[(int(r["Date"][-2:]) - 1) // 7].append(r)
    for w in sorted(weeks):
        days = sorted(x["Date"][-2:] for x in weeks[w])
        block(f"{days[0]}–{days[-1]} числа", *agg(weeks[w]))

    print("\n--- по кампаниям ---")
    camps = collections.defaultdict(list)
    for r in rows:
        camps[r["CampaignName"]].append(r)
    for name in sorted(camps):
        days = sorted(x["Date"][-2:] for x in camps[name])
        block(f"{name[:32]} ({days[0]}–{days[-1]})", *agg(camps[name]))

    print(f"\n=== ЯНДЕКС.МЕТРИКА (счётчик {args.counter}, рекламный трафик) ===")
    goal_ids = list(GOALS)
    metrics = ["ym:s:visits", "ym:s:users", "ym:s:bounceRate",
               "ym:s:pageDepth", "ym:s:avgVisitDurationSeconds"]
    metrics += [f"ym:s:goal{g}reaches" for g in goal_ids]
    data = metrika("/stat/v1/data", {
        "ids": args.counter, "metrics": ",".join(metrics),
        "filters": "ym:s:lastsignTrafficSource=='ad'",
        "date1": d1, "date2": d2, "accuracy": "full",
    })
    t = data["totals"]
    names = ["Визиты", "Посетители", "Отказы, %", "Глубина просмотра", "Время на сайте, с"]
    names += [GOALS[g] for g in goal_ids]
    for n, v in zip(names, t):
        print(f"  {n:<38}{ru(v, 0 if v == int(v) else 1):>9}")

    visits, clicks = t[0], total[1]
    if clicks:
        print(f"\n  доходимость клик → визит: {ru(visits / clicks * 100)}%  "
              f"(норма — выше 90%; ниже означает проблему со счётчиком или редиректами)")
    print("\n  ВНИМАНИЕ: цели «Отправка заявки» и «Форма отправлена» могут расходиться.")
    print("  Какая из них верна — проверяется сверкой с числом заявок Яндекса в Битриксе.")


if __name__ == "__main__":
    main()
