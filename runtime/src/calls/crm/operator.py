"""Operator hand-off: a ticket with everything the robot learned, slots pre-filtered by SQL.

The registrar's window shows the whole conversation context and the schedule;
the rows matching the caller's stated constraints (specialty / day / day part /
time) are painted green – the filter is the same SQL the scenario used, so the
operator sees exactly what the robot saw.
"""

from __future__ import annotations

import html
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Optional

from .registry import MockRegistry

SENSITIVE_KEYS = {"snils", "card_no", "birth_date", "phone", "caller_phone", "digits", "said_digits"}

USER_STATE_RU = {"calm": "спокоен", "hurried": "торопится", "agitated": "раздражён", "distressed": "плохо себя чувствует / в стрессе", "confused": "растерян"}
USER_STATE_COLOURS = {"calm": "#e3f2e1", "hurried": "#fff3cd", "agitated": "#f8d7da", "distressed": "#f5c6cb", "confused": "#d1ecf1"}


@dataclass
class OperatorTicket:
    call_id: str
    reason: str
    created_at: str
    intent: Optional[str]
    patient_id: Optional[int]
    patient_name: Optional[str]
    booking_id: Optional[int]
    constraints: dict
    utterances: list[str]
    history: list[str]
    highlight_sql: str
    highlight_params: dict
    highlighted_slot_ids: list[int]
    slots: list[dict] = field(default_factory=list)  # preview: all free slots for the specialty (first N)
    notes: list[str] = field(default_factory=list)
    user_state: Optional[dict] = None  # early caller state (calm / hurried / agitated / distressed / confused) + cues
    machine_view: Optional[dict] = None  # frozen data shown to the slot chooser; never executed
    dialogue: list[dict] = field(default_factory=list)
    request_id: Optional[int] = None  # row id in the mirror's operator_requests (sync bookkeeping with the stand)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    def _state_html(self) -> str:
        if not self.user_state or not self.user_state.get("label"):
            return ""
        label = str(self.user_state["label"])
        colour = USER_STATE_COLOURS.get(label, "#eeeeee")
        cues = ", ".join(str(c) for c in self.user_state.get("cues", []))
        early = self.user_state.get("early_intent")
        ms = self.user_state.get("decided_at_ms")
        tail = f" · ранняя оценка намерения: {html.escape(str(early))} через {ms:.0f} мс" if early and ms is not None else ""
        return (f"<p>Состояние звонящего: <span style='background:{colour};padding:2px 6px;border-radius:4px'>"
                f"<b>{html.escape(USER_STATE_RU.get(label, label))}</b></span>"
                f"{(' — ' + html.escape(cues)) if cues else ''}{tail}</p>")

    def summary(self) -> str:
        """One line for the stand's operator-request record (no personal data: ids and constraints only)."""
        bits = [f"причина: {self.reason}"]
        if self.intent:
            bits.append(f"намерение: {self.intent}")
        if self.constraints:
            bits.append("ограничения: " + ", ".join(f"{k}={v}" for k, v in self.constraints.items()))
        if self.highlighted_slot_ids:
            bits.append(f"подходящих слотов: {len(self.highlighted_slot_ids)}")
        if self.user_state and self.user_state.get("label"):
            bits.append("состояние: " + USER_STATE_RU.get(str(self.user_state["label"]), str(self.user_state["label"])))
        return "; ".join(bits)

    def to_html(self) -> str:
        rows = []
        green = set(self.highlighted_slot_ids)
        for s in self.slots:
            style = ' style="background:#c8f7c5"' if s["id"] in green else ""
            rows.append(f"<tr{style}><td>{s['id']}</td><td>{html.escape(s['doctor'])}</td><td>{html.escape(s['specialty'])}</td><td>{html.escape(str(s['starts_at']))}</td></tr>")
        cons = "".join(f"<li>{html.escape(k)}: {html.escape(str(v))}</li>" for k, v in self.constraints.items())
        notes = "".join(f"<li>{html.escape(str(note))}</li>" for note in self.notes)
        utts = "".join(f"<li>{html.escape(u)}</li>" for u in self.utterances)
        machine = ""
        if self.machine_view:
            machine = (
                "<h3>Что видела машина при последнем предложении</h3><pre>"
                + html.escape(json.dumps(self.machine_view, ensure_ascii=False, indent=2))
                + "</pre><p>Снимок на момент предложения. Доступность перед записью проверяется заново.</p>"
            )
        dialogue = "".join(
            "<li><b>" + html.escape(str(turn.get("role", ""))) + ":</b> "
            + html.escape(str(turn.get("text", ""))) + "</li>"
            for turn in self.dialogue
        )
        return (
            f"<html><head><meta charset='utf-8'><title>Звонок {html.escape(self.call_id)}</title></head><body>"
            f"<h2>Перевод на оператора: {html.escape(self.reason)}</h2>"
            f"<p>Пациент: <b>{html.escape(self.patient_name or 'не идентифицирован')}</b> (id {self.patient_id}) · запись #{self.booking_id} · намерение: {html.escape(self.intent or '—')}</p>"
            f"{self._state_html()}"
            f"<ul>{notes}</ul>"
            f"<h3>Что сказал пациент</h3><ul>{utts}</ul><h3>Ограничения</h3><ul>{cons}</ul>"
            f"<h3>Слоты (зелёным — подходящие по словам пациента)</h3><pre>{html.escape(self.highlight_sql)}\n{html.escape(json.dumps(self.highlight_params, ensure_ascii=False))}</pre>"
            f"<table border='1' cellpadding='4'><tr><th>id</th><th>врач</th><th>специальность</th><th>время</th></tr>{''.join(rows)}</table>"
            f"{machine}<h3>Разговор</h3><ol>{dialogue}</ol>"
            f"<p>Маршрут: {html.escape(' → '.join(self.history))}</p></body></html>"
        )


def build_ticket(reg: MockRegistry, call_id: str, reason: str, vars: dict, history: list[str], utterances: list[str], preview: int = 40, user_state: Optional[dict] = None, dialogue: Optional[list[dict]] = None) -> OperatorTicket:
    specialty = vars.get("specialty")
    day = vars.get("said_date") or vars.get("slot_date")
    daypart = vars.get("said_daypart")
    t_from = vars.get("said_time")
    constraints = {k: v for k, v in (("specialty", specialty), ("date", day), ("daypart", daypart), ("time", t_from), ("doctor_surname", vars.get("doctor_surname")),
                                      ("for_other", vars.get("for_other")), ("needs_referral", vars.get("needs_referral")), ("has_referral", vars.get("has_referral"))) if v not in (None, "")}
    sql, params = reg.highlight_sql(specialty, date.fromisoformat(day) if day else None, daypart, t_from)
    ids = [r["id"] for r in reg.execute(sql, params)]
    all_slots = reg.find_slots(specialty, limit=preview) if specialty else []
    slots = [{"id": s.id, "doctor": s.doctor, "specialty": s.specialty, "starts_at": s.starts_at.isoformat(timespec="minutes")} for s in all_slots]
    # make sure highlighted rows are visible even if beyond the preview window
    shown = {s["id"] for s in slots}
    for sid in ids[:10]:
        if sid not in shown:
            s = reg.slot(sid)
            if s:
                slots.append({"id": s.id, "doctor": s.doctor, "specialty": s.specialty, "starts_at": s.starts_at.isoformat(timespec="minutes")})
    machine_view = None
    # Preserve the original read-only result, including rows no longer free now.
    # This is evidence for the operator, never a new SQL execution path.
    if isinstance(vars.get("agent_sql"), str) and isinstance(vars.get("agent_sql_params"), dict) and isinstance(vars.get("agent_seen_slots"), list):
        machine_view = deepcopy({
            "query": vars["agent_sql"], "parameters": vars["agent_sql_params"],
            "slots": vars["agent_seen_slots"][:96],
            "selected_slot_id": vars.get("agent_selection"),
            "offers_made": vars.get("agent_attempt"),
            "selection_mode": vars.get("agent_mode"),
        })
    notes = []
    if vars.get("needs_referral") and not vars.get("has_referral"):
        notes.append("Требуется направление терапевта — пациент не подтвердил наличие")
    if vars.get("doctor_surname"):
        notes.append(f"Пациент просил конкретного врача: {vars['doctor_surname']}")
    if user_state and user_state.get("label") in ("agitated", "distressed"):
        notes.append(f"Звонящий {USER_STATE_RU.get(str(user_state['label']), user_state['label'])} — начните с этого, не с уточнений")
    ticket = OperatorTicket(
        call_id=call_id, reason=reason, created_at=datetime.now().isoformat(timespec="seconds"), intent=vars.get("last_intent"),
        patient_id=vars.get("patient_id"), patient_name=vars.get("patient_name"), booking_id=vars.get("booking_id"),
        constraints=constraints, utterances=[u for u in utterances if u], history=history, highlight_sql=sql, highlight_params=params,
        highlighted_slot_ids=ids, slots=slots, notes=notes, user_state=user_state,
        machine_view=machine_view, dialogue=deepcopy((dialogue or [])[-500:]),
    )
    ticket.request_id = reg.record_operator_request(call_id, reason, ticket.to_json())
    return ticket


def scrub(vars: dict) -> dict:
    """Remove explicit personal data before anything leaves the orchestrator (logs, LLM prompts)."""
    return {k: ("***" if k in SENSITIVE_KEYS else v) for k, v in vars.items()}
