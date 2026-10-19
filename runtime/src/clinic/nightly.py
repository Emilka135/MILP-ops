"""Night worker; CRM owns the relational opening-hours and calendar decision.

Each cycle attempts both independent queues. A failed preparation request cannot
prevent feedback clustering. No patient text, tokens, URLs or exception messages
are written to worker logs.
"""
import json
import time
import urllib.request

from calls.services.netutil import open_http
from .modules import CrmProxy

JOBS = (
    ("prepare_registrar_examples", "learning.night"),
    ("cluster_feedback_and_prepare_profiles", "nightly.run"),
)
MAX_RESPONSE_BYTES = 64 * 1024
RPC_TIMEOUT_SECONDS = 30
COUNT_FIELDS = {"processed", "topics", "jobs_run", "jobs_failed"}
SAFE_VALUES = {
    "reason": {"clinic_is_open"},
    "method": {"local_supervised_examples", "local_tfidf_cosine"},
    "status": {"finished"},
}


class NightCrmProxy(CrmProxy):
    """Bound the response and network wait of this unattended worker only."""

    def call(self, op, args):
        if op not in {operation for _, operation in JOBS} or args:
            raise ValueError("invalid nightly operation")
        request = urllib.request.Request(
            f"{self.url}/v1/{op}", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with open_http(request, timeout=RPC_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("nightly response exceeded limit")
        result = json.loads(raw)
        return result["status"], result["payload"]


def _safe_result(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return "error", {"error": "invalid_rpc_response"}
    status, payload = value
    if status not in {"ok", "blocked", "error", "empty", "forbidden"} or not isinstance(payload, dict):
        return "error", {"error": "invalid_rpc_response"}
    safe = {}
    for key in COUNT_FIELDS:
        number = payload.get(key)
        if type(number) is int and 0 <= number <= 1_000_000_000:
            safe[key] = number
    for key, allowed in SAFE_VALUES.items():
        text = payload.get(key)
        if isinstance(text, str) and text in allowed:
            safe[key] = text
    if status in {"error", "forbidden"}:
        safe["error"] = "crm_job_rejected"
    return status, safe


def run_once(crm):
    """Attempt both queues, returning the legacy learning-job tuple.

    Consumers historically receive ``(status, {processed, ...})``. Preserve that
    contract; the second job has its own bounded structured log record. Neither
    queue is reported as successful merely because the other queue passed.
    """
    learning_result = None
    for name, operation in JOBS:
        try:
            status, result = _safe_result(crm.call(operation, {}))
        except Exception:
            # Backend exceptions may contain access tokens, URLs or patient text.
            # The durable jobs remain queued in CRM and can retry next cycle.
            status, result = "error", {"error": "crm_rpc_failed"}
        print(json.dumps({**result, "job": name, "status": status}, ensure_ascii=False), flush=True)
        if operation == "learning.night":
            learning_result = status, result
    return learning_result


def run():
    crm = NightCrmProxy()
    while True:
        run_once(crm)
        time.sleep(60)
