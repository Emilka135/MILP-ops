"""Operator hand-off: a ticket with everything the robot learned, slots pre-filtered by SQL.

The registrar's window shows the whole conversation context and the schedule;
the rows matching the caller's stated constraints (specialty / day / day part /
time) are painted green – the filter is the same SQL the scenario used, so the
operator sees exactly what the robot saw.
"""

from __future__ import annotations

import html
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .registry import MockRegistry

SENSITIVE_KEYS = {"snils", "card_no", "birth_date", "phone", "caller_phone", "digits", "said_digits"}

USER_STATE_RU = {"calm": "спокоен", "hurried": "торопится", "agitated": "раздражён", "distressed": "плохо себя чувствует / в стрессе", "confused": "растерян"}
USER_STATE_COLOURS = {"calm": "#e3f2e1", "hurried": "#fff3cd", "agitated": "#f8d7da", "distressed": "#f5c6cb", "confused": "#d1ecf1"}

REASONS_RU = {
    "requested": "Пациент попросил соединить с регистратором",
    "three_offers_declined": "Предложенные варианты времени не подошли",
    "no_suitable_slot": "Не удалось подобрать удобное время",
    "none_suitable": "Предложенные варианты времени не подошли",
    "no_slots": "Свободные талоны не найдены",
    "no_suitable_day": "В удобный пациенту день нет подходящих талонов",
    "patient_disputes_booking": "Пациент не согласен с оформленной записью — проверьте её",
    "first_visit_registration": "Пациент обращается впервые: нужна помощь с регистрацией",
    "identity_failed": "Не удалось подтвердить, кто звонит",
    "patient_not_found": "Карточка пациента не найдена",
    "ambiguous_identity": "Найдено несколько похожих карточек пациента",
    "crm_error": "Не удалось получить подтверждение от системы записи",
    "agent_contract_error": "Помощник не смог надёжно подобрать время",
    "no_valid_input": "Не удалось понять ответ пациента",
    "speech_unclassified": "Нужно уточнить, с каким вопросом звонит пациент",
    "no_specialty": "Нужно помочь выбрать специалиста",
    "specific_doctor": "Пациент просит конкретного врача",
    "diagnostic_schedule_required": "Нужно подобрать время исследования",
    "no_diagnostic": "Нужно уточнить, какое исследование требуется",
    "referral_missing": "Пациент не подтвердил наличие направления",
    "referral_expired": "Нужно проверить срок действия направления",
    "referral_question": "Пациенту нужна помощь с направлением",
    "referral_unclear": "Не удалось уточнить сведения о направлении",
    "human_only": "Вопрос требует участия человека",
    "emergency": "Пациент сообщает о срочной ситуации",
    "feedback_requires_person": "Нужно лично разобрать отзыв пациента",
    "more_help": "После основной записи пациенту нужна дополнительная помощь",
    "not_a_date": "Нужно уточнить удобный день приёма",
    "not_a_daypart": "Нужно уточнить удобное время суток",
    "unclear": "Нужно уточнить запрос пациента",
    "internal": "Помощник не смог продолжить разговор",
}
INTENTS_RU = {
    "book": "Записаться на приём", "reschedule": "Перенести запись",
    "cancel": "Отменить запись", "confirm": "Подтвердить приём",
    "my_booking": "Узнать свою запись", "operator": "Поговорить с регистратором",
    "medical": "Врачебный вопрос", "emergency": "Срочное обращение",
    "complaint": "Разобрать жалобу", "info_schedule": "Узнать часы работы",
    "info_address": "Узнать адрес", "info_docs": "Узнать документы",
    "info_holidays": "Узнать о праздничных днях", "referral": "Уточнить направление",
}
CONSTRAINTS_RU = {
    "specialty": "Специалист", "date": "Удобный день", "daypart": "Время суток",
    "time": "Удобное время", "doctor_surname": "Нужный врач",
    "for_other": "Звонит за другого человека", "needs_referral": "Нужно направление",
    "has_referral": "Направление подтверждено",
}
DAYPARTS_RU = {"morning": "Утром", "midday": "Днём", "afternoon": "После обеда", "evening": "Вечером"}
ROLES_RU = {"user": "Пациент", "patient": "Пациент", "assistant": "Помощник", "operator": "Регистратор", "dtmf": "Клавиши", "system": "Служебное сообщение"}


def _escape(value):
    return html.escape(str(value))


def _identifier(value):
    return type(value) is int and value > 0


def _stamp(value, *, audit_time=False):
    if not value:
        return "Не указано"
    try:
        instant = datetime.fromisoformat(str(value))
        if audit_time:
            # Legacy Docker audit timestamps were naive UTC; appointment rows
            # are clinic-local and are deliberately not converted by this path.
            instant = (instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant).astimezone(timezone(timedelta(hours=3)))
        return instant.strftime("%d.%m.%Y в %H:%M")
    except (ValueError, TypeError):
        return str(value)


def _constraint_value(key, value):
    if type(value) is bool:
        return "Да" if value else "Нет"
    if key == "daypart":
        return DAYPARTS_RU.get(str(value), str(value))
    if key == "date":
        try:
            return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return str(value)


OPERATOR_CSS = """
body{margin:0;background:#f4f6f4;color:#263e35;font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.7}
.operator-ticket{max-width:1040px;margin:20px auto;padding:25px;background:white;border:1px solid #dce5df;border-radius:14px;overflow-wrap:anywhere}
.operator-ticket *{box-sizing:border-box}.operator-ticket h2{font-size:23px;line-height:1.4;margin:0 0 8px}.operator-ticket h3{font-size:18px;margin:0 0 12px}
.operator-ticket p{margin:8px 0}.operator-ticket section{border-top:1px solid #dce5df;margin-top:23px;padding-top:22px}.operator-ticket .muted{color:#63796d;font-size:13px}
.operator-ticket .reason{font-size:17px}.operator-ticket .identity,.operator-ticket .offer{background:#f1f7ef;border:1px solid #d5e4ce;border-radius:9px;padding:14px 17px;margin:17px 0}
.operator-ticket dl{display:grid;grid-template-columns:minmax(130px,1fr) minmax(160px,2fr);gap:7px 20px;margin:12px 0}.operator-ticket dt{color:#617367}.operator-ticket dd{margin:0}
.operator-ticket .dialogue{list-style:none;padding:0;margin:0}.operator-ticket .dialogue li{padding:12px 14px;margin:8px 0;background:#f5f7f4;border-left:3px solid #aac5b0;border-radius:4px}
.operator-ticket .dialogue li.patient{background:#eef5fa;border-left-color:#9abbd0}.operator-ticket .dialogue li b{display:block;font-size:12px;color:#5e7567;margin-bottom:4px}.operator-ticket .dialogue li p{white-space:pre-wrap;margin:0}
.operator-ticket details{border:1px solid #dbe4dd;padding:12px 15px;border-radius:8px;margin:15px 0}.operator-ticket summary{cursor:pointer;font-weight:600}
.operator-ticket pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:12px;line-height:1.7;background:#f4f6f3;padding:12px;border-radius:6px;max-width:100%}
.operator-ticket table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px}.operator-ticket th,.operator-ticket td{padding:10px 7px;text-align:left;border:1px solid #dce5df;overflow-wrap:anywhere}.operator-ticket th{background:#edf3ec}.operator-ticket th:first-child{width:60px}
.operator-ticket .notes{background:#fff7e6;border-radius:8px;padding:12px 20px 12px 35px}.operator-ticket .empty{color:#728075;font-size:14px}
@media(max-width:600px){.operator-ticket{margin:8px;padding:17px;border-radius:10px;font-size:14px}.operator-ticket h2{font-size:20px}.operator-ticket h3{font-size:17px}.operator-ticket dl{grid-template-columns:1fr;gap:3px}.operator-ticket dd{margin-bottom:9px}.operator-ticket th,.operator-ticket td{padding:7px 4px;font-size:11px}.operator-ticket th:first-child{width:40px}}
"""



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
        tail = ""
        if early and type(ms) in {int, float} and math.isfinite(ms) and ms >= 0:
            intent = INTENTS_RU.get(str(early), "нужно уточнить запрос")
            tail = f" · ранняя оценка: {_escape(intent)} через {ms:.0f} мс"
        return (f"<p>Состояние звонящего: <span style='background:{colour};padding:2px 6px;border-radius:4px'>"
                f"<b>{_escape(USER_STATE_RU.get(label, 'нужно уточнить при разговоре'))}</b></span>"
                f"{(' — ' + _escape(cues)) if cues else ''}{tail}</p>")

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
        """Readable handoff, with frozen evidence separate from transfer-time reads.

        Rendering never executes SQL or refreshes rows. Original machine data and
        the dataclass JSON remain unchanged for audit/backward compatibility.
        CSS lives in body because the aggregated operator list embeds that body.
        """
        reason = REASONS_RU.get(self.reason, "Помощнику нужна помощь регистратора")
        identity = [f"<p>Пациент: <b>{_escape(self.patient_name or 'Личность ещё не подтверждена')}</b></p>"]
        if _identifier(self.patient_id):
            identity.append(f"<p class='muted'>Карточка № {_escape(self.patient_id)}</p>")
        if _identifier(self.booking_id):
            identity.append(f"<p>Запись № {_escape(self.booking_id)}</p>")
        if self.intent:
            identity.append(f"<p>Запрос: {_escape(INTENTS_RU.get(self.intent, 'Нужно уточнить запрос'))}</p>")
        notes = "".join(f"<li>{_escape(note)}</li>" for note in self.notes)
        notes = f"<ul class='notes'>{notes}</ul>" if notes else ""
        machine = ""
        if self.machine_view:
            view = self.machine_view
            selected = view.get("selected_slot_id")
            snapshots = view.get("slots", [])
            selected_row = next((row for row in snapshots if isinstance(row, dict) and _identifier(selected) and row.get("id") == selected), None)
            offer = "<p>Последнее время приёма не выбрано.</p>"
            if selected_row:
                offer = f"<p><b>{_escape(_stamp(selected_row.get('starts_at')))}</b></p>"
                if selected_row.get("doctor"):
                    offer += f"<p>Врач: {_escape(selected_row['doctor'])}</p>"
                if selected_row.get("specialty"):
                    offer += f"<p>Специалист: {_escape(selected_row['specialty'])}</p>"
                offer += f"<p class='muted'>Талон № {_escape(selected)}</p>"
            elif _identifier(selected):
                offer = f"<p>Помощник предложил талон № {_escape(selected)}. Подробности времени смотрите в исходном снимке.</p>"
            attempts = view.get("offers_made")
            if type(attempts) is int and attempts >= 0:
                offer += f"<p class='muted'>Попыток подбора: {attempts}</p>"
            machine = (
                "<section class='machine-view'><h3>Последнее предложение помощника</h3>"
                f"<div class='offer'>{offer}</div>"
                "<p class='muted'>Снимок на момент предложения. Это предложенное время, а не подтверждение согласия пациента. Доступность перед записью проверяется заново.</p>"
                "<details><summary>Открыть исходные слоты и запрос</summary><pre>"
                + _escape(json.dumps(view, ensure_ascii=False, indent=2))
                + "</pre></details></section>"
            )
        dialogue = []
        for turn in self.dialogue:
            role = str(turn.get("role", ""))
            css_class = "patient" if role in {"user", "patient"} else "other"
            label = ROLES_RU.get(role, "Участник разговора")
            dialogue.append(f"<li class='{css_class}'><b>{_escape(label)}</b><p>{_escape(turn.get('text', ''))}</p></li>")
        if dialogue:
            conversation = "<ol class='dialogue'>" + "".join(dialogue) + "</ol>"
        elif self.utterances:
            conversation = "<p class='muted'>Полный разговор не сохранён. Доступны последние слова пациента.</p><ol class='dialogue'>" + "".join(f"<li class='patient'><b>Пациент</b><p>{_escape(text)}</p></li>" for text in self.utterances) + "</ol>"
        else:
            conversation = "<p class='empty'>Текст разговора пока не получен. Уточните запрос пациента.</p>"
        constraints = "".join(
            f"<dt>{_escape(CONSTRAINTS_RU.get(key, 'Дополнительное условие'))}</dt><dd>{_escape(_constraint_value(key, value))}</dd>"
            for key, value in self.constraints.items() if value is not None and value != ""
        )
        constraints = f"<dl>{constraints}</dl>" if constraints else "<p class='empty'>Предпочтения пока не уточнены.</p>"
        rows = []
        green = set(self.highlighted_slot_ids)
        for slot in self.slots:
            style = ' style="background:#c8f7c5"' if slot.get("id") in green else ""
            rows.append(f"<tr{style}><td>{_escape(slot.get('id', '—'))}</td><td>{_escape(slot.get('doctor', 'Не указан'))}</td><td>{_escape(slot.get('specialty', 'Не указан'))}</td><td>{_escape(_stamp(slot.get('starts_at')))}</td></tr>")
        table = ("<table><thead><tr><th>№</th><th>Врач</th><th>Специалист</th><th>Время</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>") if rows else "<p class='empty'>В выборке при передаче свободных талонов нет.</p>"
        preview = (
            "<section class='availability-preview'><details><summary>Свободные талоны при передаче звонка</summary>"
            f"<p class='muted'>Отдельная выборка из базы на момент передачи: {_escape(_stamp(self.created_at, audit_time=True))}. Она может отличаться от снимка помощника выше. Перед записью проверьте доступность заново. Зелёным выделены подходящие по словам пациента талоны.</p>"
            + table + "<details><summary>Запрос этой выборки и параметры</summary><pre>"
            + _escape(self.highlight_sql) + "\n" + _escape(json.dumps(self.highlight_params, ensure_ascii=False, indent=2))
            + "</pre></details></details></section>"
        )
        technical = ("<details class='technical'><summary>Технические сведения о звонке</summary><p>Исходные данные заявки и маршрут. Содержимое показано как текст и не выполняется.</p><pre>"
                     + _escape(self.to_json()) + "</pre></details>")
        return (
            "<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Звонок {_escape(self.call_id)}</title></head><body><style>{OPERATOR_CSS}</style><article class='operator-ticket'>"
            f"<header><h2>Нужна помощь регистратора</h2><p class='reason'>{_escape(reason)}</p><p class='muted'>Звонок {_escape(self.call_id)} · Передан {_escape(_stamp(self.created_at, audit_time=True))} МСК</p></header>"
            f"<div class='identity'>{''.join(identity)}{self._state_html()}</div>{notes}{machine}"
            f"<section class='conversation'><h3>Разговор с пациентом</h3>{conversation}</section>"
            f"<section class='patient-constraints'><h3>Что важно при подборе</h3>{constraints}</section>"
            f"{preview}{technical}</article></body></html>"
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
        call_id=call_id, reason=reason, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), intent=vars.get("last_intent"),
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
