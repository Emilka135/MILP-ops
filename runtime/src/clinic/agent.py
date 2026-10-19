"""Small bounded slot agent. The model never receives credentials, PII or a SQL connection."""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from calls.services.netutil import open_http, post_json
from calls.tts.verbalize import date_to_words, time_to_words

from .modules import CrmProxy
from .planner_store import SLOTS_PER_DAY, normalize_constraints

TOTAL_BUDGET_SECONDS = 24.0
CRM_TIMEOUT_SECONDS = 5.0
MODEL_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 128000


def offer_text(starts):
    """Canonical facts shared with the pronunciation preflight."""
    return f"Есть свободная запись: {date_to_words(starts.date())}, в {time_to_words(starts.time())}."


class ModelProviderError(Exception):
    def __init__(self, code=None):
        self.code = (
            code if isinstance(code, int) and not isinstance(code, bool) else None
        )
        super().__init__("model_provider_error")


def _bounded(call, seconds):
    """A wall deadline also bounds urllib's per-read timeout on a slow response.

    An expired read-only request may finish in its daemon worker. It is never
    followed by a second model request after a transport timeout.
    """
    results = queue.Queue(maxsize=1)

    def run():
        try:
            results.put((True, call()))
        except Exception as exc:  # noqa: BLE001 -- return failure to the owning request
            results.put((False, exc))

    threading.Thread(target=run, daemon=True).start()
    try:
        ok, value = results.get(timeout=max(0, seconds))
    except queue.Empty:
        raise TimeoutError("agent stage deadline") from None
    if not ok:
        raise value
    return value


SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["offer", "operator"]},
        "slot_id": {"type": ["integer", "null"]},
        "text": {"type": "string", "maxLength": 350},
    },
    "required": ["action", "slot_id", "text"],
    "additionalProperties": False,
}


def load_key(prefix="OPENROUTER"):
    """Read only the chosen provider's locally configured credential."""
    key = os.environ.get(prefix + "_API_KEY", "").strip()
    path = os.environ.get(prefix + "_API_KEY_FILE", "")
    if not key and path:
        key = Path(path).read_text().strip()
    if any(char in key for char in ("\r", "\n")):
        raise ValueError("invalid local model API key")
    return key


def completion_url(base):
    """Reject credentials/queries in administrator configuration, before HTTP."""
    parts = urlsplit(base)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (
            parts.scheme != "https"
            and not (
                parts.scheme == "http"
                and local
                and os.environ.get("ALLOW_LOCAL_LLM") == "1"
            )
        )
    ):
        raise ValueError("model base URL requires HTTPS and no embedded credentials")
    return base.rstrip("/") + "/chat/completions"


def decision_is_valid(value, slots):
    if not isinstance(value, dict) or set(value) != {"action", "slot_id", "text"}:
        return False
    if not isinstance(value["text"], str) or len(value["text"]) > 350:
        return False
    if value["action"] == "operator":
        return value["slot_id"] is None
    return (
        value["action"] == "offer"
        and isinstance(value["slot_id"], int)
        and not isinstance(value["slot_id"], bool)
        and value["slot_id"] in {s["id"] for s in slots}
    )


class SlotAgent:
    def __init__(self, crm=None, choose=None, settings=None):
        self.crm = crm or CrmProxy()
        self.mode = (('deterministic' if settings['provider']=='local' else 'openrouter' if settings['provider']=='openrouter' else 'openai')
                     if settings else os.environ.get("AGENT_MODE", "deterministic"))
        if self.mode not in {"deterministic", "openrouter", "openai"}:
            raise ValueError("unknown AGENT_MODE")
        self.key = ""
        self.model = ""
        self.url = ""
        if self.mode != "deterministic":
            prefix = "OPENROUTER" if self.mode == "openrouter" else "OPENAI"
            from .module_settings import secret
            self.key = secret(settings) if settings else load_key(prefix)
            self.model = settings['model'] if settings else os.environ.get(prefix + "_MODEL", "").strip()
            if not self.key or not self.model:
                raise ValueError(prefix + "_MODEL and local API key are required")
            base = (
                "https://openrouter.ai/api/v1"
                if self.mode == "openrouter"
                else (settings['base_url'] if settings else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
            )
            self.url = completion_url(base)
        self.choose = choose

    def model_decide(self, slots, constraints, timeout=MODEL_TIMEOUT_SECONDS):
        if self.choose:
            return self.choose(deepcopy(slots), deepcopy(constraints))
        if self.mode == "deterministic":
            return {"action": "offer", "slot_id": slots[0]["id"], "text": ""}
        # Selection needs times and opaque IDs only. Even specialty and doctor
        # names are omitted, and raw caller fields never enter this payload.
        context = {
            "task": "Выбрать один свободный слот из возвращённых БД ближайших трёх дней. Если ни один не подходит — operator. Не изобретать данные и SQL. Краткая русская фраза предложения; номер слота должен точно существовать.",
            "slots": [
                {
                    "id": s["id"],
                    "starts_at": s["starts_at"],
                }
                for s in slots
            ],
            "constraints": constraints,
        }
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 250,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты помощник регистратуры. Ответ только по схеме. Данные пациента и медицинские советы запрещены. Действие записи никогда не выполняется на этом шаге.",
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "slot_offer", "strict": True, "schema": SCHEMA},
            },
        }
        if self.mode == "openrouter":
            payload["provider"] = {"require_parameters": True, "data_collection": "deny"}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with open_http(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("model_response_too_large")
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("invalid_model_envelope")
        if data.get("error"):
            error = data["error"]
            raise ModelProviderError(
                error.get("code") if isinstance(error, dict) else None
            )
        return json.loads(data["choices"][0]["message"]["content"])

    def handle(self, op, args):
        deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        if op != "offer":
            return "error", {"error": "unknown agent operation"}
        try:
            if not isinstance(args, dict):
                raise TypeError("invalid_args")
            raw_attempt = args.get("attempt")
            if isinstance(raw_attempt, bool) or not isinstance(
                raw_attempt, (int, str, type(None))
            ):
                raise TypeError("invalid_attempt")
            attempt = int(args.get("attempt") or 0)
            if attempt < 0:
                raise ValueError("invalid_attempt")
            normalized = normalize_constraints(args)
        except (TypeError, ValueError):
            return "error", {
                "error": "invalid_agent_constraints",
                "agent_mode": self.mode,
            }
        if attempt >= 3:
            return "exhausted", {"reason": "three_offers_declined"}
        constraints = {
            k: normalized[k]
            for k in ("said_date", "said_time", "said_daypart")
            if normalized.get(k)
        }
        lookup = {
            "patient_id": args.get("patient_id"),
            **normalized,
            "referral_verified": args.get("referral_verified") is True,
        }
        crm_timeout = min(CRM_TIMEOUT_SECONDS, max(0, deadline - time.monotonic()))
        try:

            def find_slots():
                if isinstance(self.crm, CrmProxy):
                    result = post_json(
                        f"{self.crm.url}/v1/agent_slots", lookup, timeout=crm_timeout
                    )
                    return result["status"], result["payload"]
                return self.crm.call("agent_slots", lookup)

            status, found = _bounded(find_slots, crm_timeout)
        except TimeoutError:
            return "error", {"error": "crm_timeout", "agent_mode": self.mode}
        except (OSError, ValueError, KeyError, TypeError):
            return "error", {"error": "crm_lookup_failed", "agent_mode": self.mode}
        if status != "ok":
            return status, found
        slots = found.get("slots") if isinstance(found, dict) else None
        if not isinstance(slots, list) or len(slots) > 3 * SLOTS_PER_DAY:
            return "error", {"error": "invalid_slot_response", "agent_mode": self.mode}
        if not slots:
            return "empty", found
        try:
            ids = [s["id"] for s in slots]
            starts = [datetime.fromisoformat(s["starts_at"]) for s in slots]
            days = {stamp.date() for stamp in starts}
            if any(stamp.tzinfo is not None for stamp in starts):
                raise ValueError("slot_timestamp_must_use_clinic_wall_time")
            for stamp in starts:
                if sum(other.date() == stamp.date() for other in starts) > SLOTS_PER_DAY:
                    raise ValueError("too_many_slots_for_day")
            if any(sid in normalized["excluded"] for sid in ids):
                raise ValueError("excluded_slot_returned")
            if (
                len(days) > 3
                or len(set(ids)) != len(ids)
                or any(
                    isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0
                    for sid in ids
                )
            ):
                raise ValueError("invalid_slot_ids_or_days")
            if not isinstance(found["sql"], str) or not isinstance(
                found["params"], dict
            ):
                raise TypeError("missing_local_query_context")
        except (ValueError, KeyError, TypeError):
            return "error", {"error": "invalid_slot_response", "agent_mode": self.mode}
        choice = None
        for model_attempt in range(2):
            remaining = min(MODEL_TIMEOUT_SECONDS, max(0, deadline - time.monotonic()))
            if remaining <= 0:
                return "error", {
                    "error": "agent_budget_exhausted",
                    "agent_mode": self.mode,
                    "model_attempts": model_attempt,
                }
            try:
                proposed = _bounded(
                    lambda stage_timeout=remaining: self.model_decide(
                        slots, constraints, timeout=stage_timeout
                    ),
                    remaining,
                )
                if decision_is_valid(proposed, slots):
                    choice = proposed
                    break
            except urllib.error.HTTPError as exc:
                return "error", {
                    "error": "model_http_error",
                    "http_status": exc.code,
                    "agent_mode": self.mode,
                    "model_attempts": model_attempt + 1,
                }
            except ModelProviderError as exc:
                return "error", {
                    "error": "model_provider_error",
                    "provider_code": exc.code,
                    "agent_mode": self.mode,
                    "model_attempts": model_attempt + 1,
                }
            except TimeoutError:
                return "error", {
                    "error": "model_timeout",
                    "agent_mode": self.mode,
                    "model_attempts": model_attempt + 1,
                }
            except OSError:
                return "error", {
                    "error": "model_transport_error",
                    "agent_mode": self.mode,
                    "model_attempts": model_attempt + 1,
                }
            except (ValueError, KeyError, IndexError, TypeError):
                # Invalid envelope/JSON is a model-contract failure, not a successful
                # deterministic fallback. Only these bounded contract failures retry.
                continue
        if choice is None:
            return "error", {
                "error": "model_contract_rejected",
                "agent_mode": self.mode,
                "model_attempts": 2,
            }
        if choice["action"] == "operator":
            return "exhausted", {"reason": "model_cannot_offer"}
        selected = next(s for s in slots if s["id"] == choice["slot_id"])
        starts = datetime.fromisoformat(selected["starts_at"])
        # Dates, doctor and status always come from verified data, never from unconstrained model prose.
        text = offer_text(starts)
        excluded = list(normalized["excluded"])
        excluded.append(selected["id"])
        return "ok", {
            "slot_id": selected["id"],
            "agent_text": text,
            "agent_attempt": attempt + 1,
            "agent_excluded": excluded,
            "agent_mode": self.mode,
            "model_text": choice["text"],
            "agent_completed": False,
            "slot_date": starts.date().isoformat(),
            "slot_time": starts.strftime("%H:%M"),
            "slot_date_spoken": date_to_words(starts.date()),
            "slot_time_spoken": time_to_words(starts.time()),
            "agent_sql": found["sql"],
            "agent_sql_params": found["params"],
            "agent_seen_slots": slots,
            "agent_selection": choice["slot_id"],
        }
