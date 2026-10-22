"""Recover only from recorded runtime MIS failures. Never grant a model write access."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

SCHEMA = """
CREATE TABLE IF NOT EXISTS transaction_failures(
 call_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL, state TEXT NOT NULL,
 error_code TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS booking_requests(
 call_id TEXT PRIMARY KEY, patient_id INTEGER, phone TEXT NOT NULL,
 context TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 contact_policy TEXT NOT NULL DEFAULT 'sms_after_confirmed_booking');
"""

TRANSFER_MESSAGE = "Что-то пошло не так, сейчас переключу на оператора."
NIGHT_MESSAGE = "Оператора сейчас нет на месте. Запись временно недоступна. Хотите, чтобы регистратор вам перезвонил?"
REASONING_NOTICE = "Оператора сейчас нет на месте. Вашим запросом занимается ИИ, пожалуйста, подождите."


def reasoning_allowed(reasoner):
    return bool(reasoner and os.environ.get("NIGHT_REASONING_ENABLED") == "1"
                and os.environ.get("CLINIC_RUNTIME_MODE") == "production"
                and not any(os.environ.get(k, "").lower() not in {"", "0", "false"}
                            for k in ("CI", "GITHUB_ACTIONS", "ACT")))


def route(crm, args, at):
    rows = crm.reg._q("SELECT * FROM transaction_failures WHERE call_id=?", (str(args.get("call_id") or ""),))
    if not rows:
        return "form_error", {"recovery_message": "Не удалось проверить данные. Передам ваш вопрос регистратору."}
    failure = dict(rows[0])
    payload = {"recovery_message": TRANSFER_MESSAGE, "operation_state": failure["state"]}
    if os.environ.get("OPERATOR_AVAILABLE", "1") == "1" and crm.hours.is_open(crm.reg, at):
        return "operator", payload
    payload["recovery_message"] = NIGHT_MESSAGE
    if args.get("analyze") and reasoning_allowed(crm.recovery_reasoner):
        # One analysis per recorded failure, no HTTP/SQL credentials or write tools.
        key = failure["operation_key"]
        with crm.recovery_analysis_lock:
            if key in crm.recovery_analyses or len(crm.recovery_analyses) >= 1024:
                return "callback", payload
            if not crm.recovery_analysis_budget.acquire(blocking=False):
                return "callback", payload
            crm.recovery_analyses.add(key)
        context = args.get("context", {})
        encoded = json.dumps(context, ensure_ascii=False)
        bounded = json.loads(encoded) if len(encoded) <= 16000 else {}
        operations = crm.reg._q("SELECT op,args FROM operations WHERE key=?", (key,))
        operation = {"operation": operations[0]["op"], "arguments": json.loads(operations[0]["args"])} if operations else {}
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(crm.recovery_reasoner, {
            "failure": failure, "request": operation, "dialogue": bounded,
            "operator_available": False, "callback_window": "10:00–16:00",
            "allowed_actions": ["callback", "request_information"],
            "writes_forbidden": True, "retry_forbidden": True,
        })
        future.add_done_callback(lambda _: crm.recovery_analysis_budget.release())
        try:
            result = future.result(timeout=2)
            # A model may only select a fixed clarification. Free text is never spoken.
            field = result.get("missing_field") if isinstance(result, dict) else None
            if (isinstance(result, dict) and result.get("action") == "request_information"
                    and failure["state"] != "uncertain" and field in {"birth_date", "middle_name"}):
                payload["recovery_question"] = {"birth_date": "Уточните, пожалуйста, вашу дату рождения.",
                                                "middle_name": "Уточните, пожалуйста, ваше отчество."}[field]
                return "clarify", payload
        except Exception:
            pass
        finally:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
    payload["reasoning_available"] = reasoning_allowed(crm.recovery_reasoner)
    return "callback", payload
