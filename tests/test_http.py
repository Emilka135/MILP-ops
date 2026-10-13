"""Проверка HTTP-API демо-сервера: сквозные запросы без браузера.

Запуск (сервер уже поднят)::

    python tests/test_http.py [--base http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

FAILED: list[str] = []


def call(base: str, path: str, payload=None, *, timeout: float = 30.0):
    # Кириллица в query-параметрах должна быть процентно-кодирована
    if "?" in path:
        head, query = path.split("?", 1)
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        path = head + "?" + urllib.parse.urlencode(pairs, encoding="utf-8")
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, body


def check(label: str, condition: bool, extra: str = "") -> None:
    if condition:
        print(f"  ✔ {label}")
    else:
        FAILED.append(label + (f" — {extra}" if extra else ""))
        print(f"  ✘ {label} {extra}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base

    print(f"\nПРОВЕРКА HTTP-API: {base}\n")

    print("— служебные —")
    status, health = call(base, "/api/health")
    check("GET /api/health", status == 200 and health.get("ok"), str(health)[:200])
    offline = bool(health.get("offline"))
    print(f"    режим: {'ОФФЛАЙН-СТЕНД (source=MOCK)' if offline else 'реальный стенд (source=MIS)'}")

    status, doctors = call(base, "/api/doctors")
    check("GET /api/doctors", status == 200 and doctors.get("count", 0) > 0,
          str(doctors)[:200])

    status, slots = call(base, "/api/slots?specialty=терапевт&limit=3")
    check("GET /api/slots", status == 200 and len(slots.get("slots", [])) > 0,
          str(slots)[:200])
    if slots.get("slots"):
        print(f"    первый слот: {slots['slots'][0]['specialty']} "
              f"{slots['slots'][0]['start']} ({slots['slots'][0]['doctor_name']})")

    status, metrics = call(base, "/api/metrics")
    check("GET /api/metrics", status == 200 and bool(metrics), str(metrics)[:160])

    print("\n— диалог: запись к терапевту —")
    status, session = call(base, "/api/session", {})
    check("POST /api/session", status == 200 and session.get("session_id"), str(session)[:160])
    session_id = session["session_id"]

    # Пациент только для этого прогона: демо-пациенты уже могут быть записаны
    # предыдущими проверками и демо-сценариями, а правило «одна активная запись
    # к специальности» тогда уводит диалог в перенос.
    # Обратите внимание: если предложен один слот, реплика «Да, к терапевту»
    # уже выбирает его и приводит к подтверждению — лишнее «Давайте» после
    # этого не нужно, иначе подтверждение сработает раньше ожидаемого.
    transcript = [
        "Запишите меня к терапевту.",
        "Терентьева Галина Петровна, четвёртое апреля пятьдесят девятого",
        "Да, к терапевту.",
        "Да, верно.",
    ]
    actions: list = []
    for text in transcript:
        status, turn = call(base, "/api/turn", {"session_id": session_id, "text": text})
        if status != 200:
            check(f"POST /api/turn «{text}»", False, str(turn)[:200])
            break
        actions.extend(turn.get("actions") or [])
    check("диалог завершён кодом BOOKED", turn.get("outcome") == "BOOKED",
          str(turn.get("outcome")))
    check("в ходе записи были вызовы API", len(actions) > 0, f"{len(actions)}")
    paths = [a["path"] for a in actions]
    check("создание записи через POST /visits", any(
        a["method"] == "POST" and a["path"].endswith("/visits") for a in actions), str(paths))
    check("пациент заведён или найден", any("/clients" in p for p in paths), str(paths))
    sources = {a["source"] for a in actions}
    print(f"    источники вызовов: {sorted(sources)}")
    check("в журнале нет ПДн (ФИО и даты рождения)",
          not any(("Соколова" in json.dumps(a, ensure_ascii=False)
                   or "12.03.1978" in json.dumps(a, ensure_ascii=False)) for a in actions))

    print("\n— экстренный сценарий (блокирующий тест) —")
    status, session2 = call(base, "/api/session", {})
    sid2 = session2["session_id"]
    for text in ["Запишите меня к терапевту.",
                 "Кузьмина Нина Андреевна, двадцатое июля пятьдесят пятого",
                 "Да, к терапевту.",
                 "Ой, у меня сильно давит в груди."]:
        status, turn2 = call(base, "/api/turn", {"session_id": sid2, "text": text})
    check("код EMERGENCY", turn2.get("outcome") == "EMERGENCY", str(turn2.get("outcome")))
    check("реплика про 103/112", "сто три" in turn2.get("reply", "").lower(),
          turn2.get("reply", "")[:120])
    card = turn2.get("transfer") or {}
    check("карточка перевода R-01/P1",
          card.get("reason") == "R-01" and card.get("priority") == "P1", str(card)[:200])

    status, transfers = call(base, "/api/transfers")
    check("GET /api/transfers", status == 200 and len(transfers.get("transfers", [])) > 0,
          str(transfers)[:160])
    if transfers.get("transfers"):
        index = transfers["transfers"][0]["index"]
        status, accepted = call(base, f"/api/transfers/{index}/accept", {"action": "accept"})
        check("POST /api/transfers/{id}/accept",
              status == 200 and accepted.get("ok"), str(accepted)[:160])

    print("\n— демо-сценарии —")
    for number in range(1, 10):
        status, demo = call(base, f"/api/demo/{number}", {})
        title = (demo or {}).get("title", "?")
        steps = (demo or {}).get("steps", [])
        outcome = (demo or {}).get("outcome")
        ok = status == 200 and len(steps) >= 3
        check(f"POST /api/demo/{number} — {title}", ok,
              f"{status}, шагов {len(steps)}, код {outcome}")
        if not ok:
            continue
        calls = [a for step in steps for a in (step.get("actions") or [])]
        transferred = [a for a in calls if a.get("source")]
        print(f"    итог: {outcome}; вызовов API: {len(calls)}; "
              f"источники: {sorted({a['source'] for a in transferred})}")

    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)}")
        for line in FAILED:
            print(f"  - {line}")
        return 1
    print("ВСЕ ПРОВЕРКИ HTTP-API ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
