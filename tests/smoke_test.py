"""Smoke test for a running server. It DELETES both collections in MONGO_DB, so use a scratch database.

Usage:
  uvicorn app.main:app --port 8000          (in another terminal, same MONGO_URI / MONGO_DB)
  python tests/smoke_test.py                (add --big to also load 100k logs and check explain plans)
"""
import json
import os
import random
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.getenv("BASE_URL", "http://localhost:8000")
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
failures = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))
    if not ok:
        failures.append(name)


def half_up(value, places):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def millis(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


# Seed data

def log(code, day, status="PRESENT", late=0, ot=0, hours=None, half=False):
    return {"emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None,
            "work_hours": hours, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}


def employee(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": "Name " + code, "email": f"{code}@x.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


def seed_scenario():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([
        employee("EMP0001", "Eng", "2026-01-05"), employee("EMP0002", "Eng", "2026-07-15"),
        employee("EMP0003", "Eng", "2026-01-05"), employee("EMP0004", "Eng", "2026-08-01"),
        employee("EMP0005", "Ops", "2026-01-05"), employee("EMP0006", "Ops", "2026-01-05"),
        employee("EMP0007", "Ops", "2026-01-05"), employee("EMP0008", "Ops", "2026-01-05"),
        employee("EMP0009", "Sales", "2026-01-05"), employee("EMP0010", "Ops", "2026-01-05", "22:00", "06:00"),
    ])
    rng = random.Random(7)
    logs = []
    for code in ("EMP0001", "EMP0002"):
        first = date(2026, 7, 1) if code == "EMP0001" else date(2026, 7, 15)
        for i in range(31):
            day = date(2026, 7, 1) + timedelta(days=i)
            if day < first or rng.random() < 0.15:
                continue  # missing days and pre-join days
            if day.weekday() >= 5 and rng.random() < 0.6:
                continue
            status = rng.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            if status in PRESENT:
                hours = round(rng.uniform(3, 10), 2)
                logs.append(log(code, day.isoformat(), status, rng.choice([0, 0, 12, 35]), rng.choice([0, 45]), hours, hours < 4.5))
            else:
                logs.append(log(code, day.isoformat(), status))
    for code, total in (("EMP0005", 120), ("EMP0006", 100), ("EMP0007", 100), ("EMP0008", 80)):
        logs.append(log(code, "2026-07-06", late=total - 20, hours=8.0))
        logs.append(log(code, "2026-07-11", late=20, hours=8.0))  # a Saturday still counts for lateness
    logs.append(log("EMP0999", "2026-07-06", late=500, hours=8.0))  # unknown employee, must be ignored
    logs.append(log("EMP0010", "2026-07-06", late=0, ot=40, hours=8.75))
    db.attendance_logs.insert_many(logs)


# Reference implementations (plain Python, independent of the pipelines)

def weekday(day):
    return date.fromisoformat(day).weekday() < 5


def weight(doc):
    return 0.0 if doc["status"] not in PRESENT else (0.5 if doc.get("half_day") else 1.0)


def month_range(month):
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def month_logs(month, codes=None):
    a, b = (d.isoformat() for d in month_range(month))
    return [d for d in db.attendance_logs.find({"date": {"$gte": a, "$lte": b}}) if codes is None or d["emp_code"] in codes]


def ref_monthly(code, month):
    emp = db.employees.find_one({"emp_code": code})
    first, last = month_range(month)
    first = max(first, date.fromisoformat(emp["joined_on"]))
    working = sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5) if last >= first else 0
    logs = month_logs(month, {code})
    present = sum(weight(d) for d in logs if weekday(d["date"]))
    return {"working_days": working, "present_days": half_up(present, 2), "leave_days": sum(d["status"] == "LEAVE" for d in logs),
            "late_count": sum(d["late_minutes"] > 0 for d in logs), "total_late_minutes": sum(d["late_minutes"] for d in logs),
            "total_overtime_minutes": sum(d["overtime_minutes"] for d in logs),
            "attendance_pct": half_up(Decimal(str(present)) * 100 / working, 2) if working else None}


def ref_summary(month, dept=None):
    _, last = month_range(month)
    out = []
    for name in sorted({e["department"] for e in db.employees.find()}):
        if dept and name != dept:
            continue
        emps = [e["emp_code"] for e in db.employees.find({"department": name, "joined_on": {"$lte": last.isoformat()}})]
        if not emps:
            continue
        logs = month_logs(month, set(emps))
        hours = [Decimal(str(d["work_hours"])) for d in logs if d["status"] in PRESENT and d.get("work_hours") is not None]
        out.append({"department": name, "headcount": len(emps), "present_days": half_up(sum(weight(d) for d in logs if weekday(d["date"])), 2),
                    "avg_work_hours": half_up(sum(hours) / len(hours), 2) if hours else None,
                    "late_count": sum(d["late_minutes"] > 0 for d in logs), "total_late_minutes": sum(d["late_minutes"] for d in logs),
                    "leave_count": sum(d["status"] == "LEAVE" for d in logs), "on_duty_count": sum(d["status"] == "ON_DUTY" for d in logs)})
    return out


def ref_leaderboard(month, limit, dept=None):
    emps = {e["emp_code"]: e for e in db.employees.find()}
    totals = {}
    for d in month_logs(month):
        if d["emp_code"] in emps and d["late_minutes"] > 0 and (not dept or emps[d["emp_code"]]["department"] == dept):
            t = totals.setdefault(d["emp_code"], [0, 0])
            t[0] += d["late_minutes"]
            t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ranked = [{"rank": 1 + sum(1 for _, o in rows if o[0] > v[0]), "emp_code": c, "name": emps[c]["name"],
               "department": emps[c]["department"], "total_late_minutes": v[0], "late_count": v[1]} for c, v in rows]
    return [r for r in ranked if r["rank"] <= limit]


def ref_trend(dept, start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    emps = list(db.employees.find({"department": dept}))
    codes = {e["emp_code"] for e in emps}
    logs = [d for d in db.attendance_logs.find({"date": {"$gte": start, "$lte": end}}) if d["emp_code"] in codes]
    rows, rates = [], []
    for i in range((last - first).days + 1):
        day = first + timedelta(days=i)
        iso = day.isoformat()
        headcount = sum(e["joined_on"] <= iso for e in emps)
        day_logs = [d for d in logs if d["date"] == iso]
        present = sum(weight(d) for d in day_logs)
        working = day.weekday() < 5
        rate = half_up(Decimal(str(present)) / headcount, 4) if working and headcount > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({"date": iso, "is_working_day": working, "headcount": headcount, "present_count": present,
                     "late_count": sum(d["late_minutes"] > 0 for d in day_logs), "attendance_rate": rate,
                     "moving_avg_7d": half_up(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None})
    return rows


def close(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 1e-9
    return a == b


def same_rows(got, want):
    return len(got) == len(want) and all(g.keys() == w.keys() and all(close(g[k], w[k]) for k in w) for g, w in zip(got, want))


# Tests

def test_analytics():
    seed_scenario()
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0009"):
        status, got = call("GET", f"/analytics/employees/{code}/monthly?month=2026-07")
        want = {"emp_code": code, "month": "2026-07", **ref_monthly(code, "2026-07")}
        check(f"monthly {code}", status == 200 and same_rows([got], [want]), (got, want))
    check("monthly unknown employee 404", call("GET", "/analytics/employees/EMP7777/monthly?month=2026-07")[0] == 404)
    for month in ("2026-07", "2026-08", "2025-12"):
        for dept in (None, "Ops"):
            q = f"/analytics/departments/summary?month={month}" + (f"&department={dept}" if dept else "")
            status, got = call("GET", q)
            check(f"summary {month} {dept}", status == 200 and same_rows(got["items"], ref_summary(month, dept)), (got, ref_summary(month, dept)))
    for limit in (1, 2, 3, 4, 10):
        status, got = call("GET", f"/analytics/leaderboard/late?month=2026-07&limit={limit}")
        check(f"leaderboard limit={limit}", status == 200 and same_rows(got["items"], ref_leaderboard("2026-07", limit)), (got, ref_leaderboard("2026-07", limit)))
    status, got = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=2&department=Ops")
    check("leaderboard tie at cutoff returns 3 rows", [r["rank"] for r in got["items"]] == [1, 2, 2], got)
    status, got = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=4&department=Ops")
    check("leaderboard ranks 1,2,2,4", [r["rank"] for r in got["items"]] == [1, 2, 2, 4], got)
    status, got = call("GET", "/analytics/leaderboard/late?month=2026-07&department=Eng&limit=10")
    check("leaderboard department filter", same_rows(got["items"], ref_leaderboard("2026-07", 10, "Eng")), got)
    for dept, a, b in (("Eng", "2026-07-01", "2026-07-31"), ("Eng", "2026-06-25", "2026-08-05"), ("Ops", "2026-07-06", "2026-07-06"), ("Sales", "2026-07-01", "2026-07-14")):
        status, got = call("GET", f"/analytics/departments/{dept}/trend?from={a}&to={b}")
        want = ref_trend(dept, a, b)
        check(f"trend {dept} {a}..{b} ({len(want)} rows)", status == 200 and same_rows(got["items"], want),
              next(((g, w) for g, w in zip(got.get("items", []), want) if not same_rows([g], [w])), got))
    check("trend unknown department 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-05")[0] == 404)
    check("trend 93 days 422", call("GET", "/analytics/departments/Eng/trend?from=2026-07-01&to=2026-10-01")[0] == 422)
    check("trend 92 days ok", call("GET", "/analytics/departments/Eng/trend?from=2026-07-01&to=2026-09-30")[0] == 200)


def run_parallel(fn, n=12):
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(fn()[0])) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return sorted(codes)


def test_concurrency():
    db.employees.delete_many({"emp_code": {"$in": ["EMP0500", "EMP0501"]}})
    db.attendance_logs.delete_many({"emp_code": "EMP0501"})
    emp = {"emp_code": "EMP0500", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-05"}
    res = run_parallel(lambda: call("POST", "/employees", emp))
    check("parallel create: one 201, rest 409", res.count(201) == 1 and res.count(409) == len(res) - 1, res)
    call("POST", "/employees", {**emp, "emp_code": "EMP0501"})
    pin = {"emp_code": "EMP0501", "punched_at": millis(2026, 7, 6, 9, 30)}
    res = run_parallel(lambda: call("POST", "/attendance/punch-in", pin))
    check("parallel punch-in: one 201, rest 409", res.count(201) == 1 and res.count(409) == len(res) - 1, res)
    pout = {"emp_code": "EMP0501", "punched_at": millis(2026, 7, 6, 19, 5)}
    res = run_parallel(lambda: call("POST", "/attendance/punch-out", pout))
    check("parallel punch-out: one 200, rest 409", res.count(200) == 1 and res.count(409) == len(res) - 1, res)
    counter = iter(range(1, 100))
    lock = threading.Lock()

    def correct():
        with lock:
            k = next(counter)
        return call("PATCH", "/attendance/EMP0501/2026-07-06", {"punch_out": millis(2026, 7, 6, 19, 5) + k * 60000, "reason": "concurrent fix", "regularized_by": "hr"})

    res = run_parallel(correct, 6)
    _, rec = call("GET", "/attendance?emp_code=EMP0501")
    check("parallel corrections keep every history entry", len(rec["items"][0]["history"]) == res.count(200) and res.count(200) >= 1, (res, rec))


def test_explain():
    q = {"attendance_list": "?date_from=2026-07-01&date_to=2026-07-31&status=PRESENT&page=2&page_size=50",
         "employee_monthly": "?emp_code=EMP0001&month=2026-07", "department_summary": "?month=2026-07",
         "late_leaderboard": "?month=2026-07&limit=10", "department_trend": "?department=Eng&from=2026-07-01&to=2026-07-31"}
    for name, params in q.items():
        status, out = call("GET", f"/admin/explain/{name}{params}")
        text = json.dumps(out)
        check(f"explain {name}: IXSCAN and no COLLSCAN", status == 200 and "IXSCAN" in text and "COLLSCAN" not in text, text[:300] if status != 200 else "")


def load_big():
    print("loading 100k logs ...")
    db.attendance_logs.delete_many({})
    db.employees.delete_many({})
    depts = ["Eng", "Ops", "Sales", "HR", "Finance"]
    db.employees.insert_many([employee(f"EMP{i:04d}", depts[i % 5], "2026-01-05") for i in range(1, 1001)])
    rng = random.Random(3)
    batch = []
    for i in range(1, 1001):
        for d in range(100):
            day = (date(2026, 5, 1) + timedelta(days=d)).isoformat()
            batch.append(log(f"EMP{i:04d}", day, "PRESENT", rng.choice([0, 0, 0, 15]), 0, 8.0))
    db.attendance_logs.insert_many(batch)
    call("GET", "/health")


if __name__ == "__main__":
    print("health:", call("GET", "/health"))
    test_analytics()
    test_concurrency()
    if "--big" in sys.argv:
        load_big()
        test_explain()
    else:
        test_explain()
    print("\nFAILED: " + ", ".join(failures) if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
