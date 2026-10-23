"""Operator must receive the original machine view, even when availability changed."""
import json
from datetime import date, datetime

from calls.crm.operator import OperatorTicket, build_ticket
from calls.crm.registry import MockRegistry, seed_demo


def test_handoff_retains_original_rows_and_dialogue_without_executing_query(tmp_path):
    reg = MockRegistry(str(tmp_path / "handoff.sqlite"), datetime(2026, 10, 15, 11))
    seed_demo(reg, date(2026, 10, 16))
    rows = [{"id": 1, "doctor": "Тестовый врач", "specialty": "терапевт", "starts_at": "2026-10-16T08:00"}]
    variables = {"patient_id": 3, "specialty": "терапевт", "agent_seen_slots": rows,
                 "agent_sql": "SELECT :specialty; -- never execute this diagnostic string",
                 "agent_sql_params": {"specialty": "терапевт"}, "agent_selection": 1,
                 "agent_attempt": 3, "agent_mode": "deterministic"}
    dialogue = [{"role": "user", "text": "Мне нужно позже"}, {"role": "assistant", "text": "Есть свободная запись"}]
    # The row seen by the chooser has become unavailable before escalation.
    reg._x("UPDATE slots SET status='booked' WHERE id=1")
    ticket = build_ticket(reg, "handoff", "three_offers_declined", variables, ["assisted.offer"], [], dialogue=dialogue)
    assert ticket.machine_view["slots"] == rows
    assert ticket.machine_view["selected_slot_id"] == 1
    assert ticket.machine_view["offers_made"] == 3
    assert ticket.machine_view["query"] == variables["agent_sql"]
    assert ticket.dialogue == dialogue
    rows[0]["doctor"] = "CHANGED"
    dialogue[0]["text"] = "CHANGED"
    variables["agent_sql_params"]["specialty"] = "CHANGED"
    assert ticket.machine_view["slots"][0]["doctor"] == "Тестовый врач"
    assert ticket.machine_view["parameters"]["specialty"] == "терапевт"
    assert ticket.dialogue[0]["text"] == "Мне нужно позже"
    stored = json.loads(reg.execute("SELECT payload FROM operator_requests")[0]["payload"])
    assert stored["machine_view"] == ticket.machine_view
    assert stored["dialogue"] == ticket.dialogue
    rendered = ticket.to_html()
    assert "Мне нужно позже" in rendered and "Снимок на момент предложения" in rendered
    assert "<details><summary>Открыть исходные слоты и запрос</summary>" in rendered
    assert OperatorTicket(**json.loads(ticket.to_json())).machine_view == ticket.machine_view


def test_diagnostic_and_dialogue_html_are_escaped_and_legacy_ticket_works(tmp_path):
    reg = MockRegistry(str(tmp_path / "legacy.sqlite"), datetime(2026, 10, 15, 11))
    seed_demo(reg, date(2026, 10, 16))
    ticket = build_ticket(reg, "legacy", "requested", {}, [], [])
    assert ticket.machine_view is None and ticket.dialogue == []
    old = json.loads(ticket.to_json())
    del old["machine_view"], old["dialogue"]
    assert OperatorTicket(**old).dialogue == []
    ticket.machine_view = {"query": "<script>bad()</script>"}
    ticket.dialogue = [{"role": "<img>", "text": "<script>bad()</script>"}]
    rendered = ticket.to_html()
    assert "<script>" not in rendered and "<img>" not in rendered
    assert "&lt;script&gt;" in rendered


class ReadableHTML:
    """Collect initial visible text; closed technical details do not count."""
    def __init__(self, markup):
        from html.parser import HTMLParser
        owner = self
        self.text = []
        self.details = []
        self.tables_outside_details = 0
        self.tags = []
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.closed = 0
                self.detail_stack = []
                self.in_style = False
            def handle_starttag(self, tag, attrs):
                owner.tags.append((tag, dict(attrs)))
                if tag == "style":
                    self.in_style = True
                if tag == "details":
                    closed = "open" not in dict(attrs)
                    owner.details.append(dict(attrs))
                    self.detail_stack.append(closed)
                    self.closed += int(closed)
                if tag == "table" and not self.closed:
                    owner.tables_outside_details += 1
            def handle_endtag(self, tag):
                if tag == "style":
                    self.in_style = False
                if tag == "details":
                    self.closed -= int(self.detail_stack.pop())
            def handle_data(self, text):
                if not self.closed and not self.in_style:
                    owner.text.append(text)
        Parser().feed(markup)
        self.visible = " ".join(self.text)


def simple_ticket(**overrides):
    values = dict(call_id="test-call", reason="three_offers_declined", created_at="2026-10-19T10:00", intent="book", patient_id=None, patient_name=None, booking_id=None, constraints={}, utterances=[], history=[], highlight_sql="SELECT original_preview", highlight_params={}, highlighted_slot_ids=[])
    values.update(overrides)
    return OperatorTicket(**values)


def test_operator_reads_real_offer_and_dialogue_before_collapsed_diagnostics():
    machine = {"selected_slot_id": 7, "offers_made": 3, "slots": [{"id": 7, "doctor": "Доктор Петров", "specialty": "терапевт", "starts_at": "2026-10-21T16:20"}], "query": "SELECT frozen_machine_query", "parameters": {"exact": "unchanged"}, "selection_mode": "deterministic"}
    ticket = simple_ticket(machine_view=machine, dialogue=[{"role": "assistant", "text": "Могу предложить среду"}, {"role": "user", "text": "Мне нужно позже"}], slots=[{"id": 8, "doctor": "Другой врач", "specialty": "терапевт", "starts_at": "2026-10-22T08:00"}])
    before = ticket.to_json()
    markup = ticket.to_html()
    parsed = ReadableHTML(markup)
    assert "Предложенные варианты времени не подошли" in parsed.visible
    assert "Доктор Петров" in parsed.visible and "21.10.2026 в 16:20" in parsed.visible
    assert "а не подтверждение согласия пациента" in parsed.visible
    assert "Мне нужно позже" in parsed.visible
    assert "Пациент" in parsed.visible and "Помощник" in parsed.visible
    assert "Другой врач" not in parsed.visible
    assert "SELECT frozen_machine_query" not in parsed.visible
    assert "SELECT original_preview" not in parsed.visible
    assert markup.index("class='machine-view'") < markup.index("class='conversation'") < markup.index("class='availability-preview'")
    assert parsed.tables_outside_details == 0
    assert parsed.details and all("open" not in attrs for attrs in parsed.details)
    assert ticket.to_json() == before


def test_constraint_labels_are_human_and_unknown_ids_do_not_leak_none_or_false():
    ticket = simple_ticket(patient_id=False, booking_id=None, constraints={"date": "2026-10-21", "daypart": "afternoon", "time": "16:20", "doctor_surname": "Петров", "for_other": False, "needs_referral": True, "has_referral": False})
    visible = ReadableHTML(ticket.to_html()).visible
    assert "Удобный день" in visible and "21.10.2026" in visible
    assert "Время суток" in visible and "После обеда" in visible
    assert "Звонит за другого человека" in visible and "Нет" in visible
    assert "Направление подтверждено" in visible and "Нужно направление" in visible
    assert "Личность ещё не подтверждена" in visible
    assert "None" not in visible and "False" not in visible and "#None" not in visible
    for raw in ("three_offers_declined", "doctor_surname", "for_other", "afternoon", "needs_referral"):
        assert raw not in visible


def test_known_identifiers_render_only_when_available_and_no_identity_is_invented():
    ticket = simple_ticket(patient_id=3, patient_name="Иван Иванович", booking_id=42)
    visible = ReadableHTML(ticket.to_html()).visible
    assert "Иван Иванович" in visible and "Карточка № 3" in visible and "Запись № 42" in visible
    missing = ReadableHTML(simple_ticket().to_html()).visible
    assert "Карточка №" not in missing and "Запись №" not in missing
    assert "Текст разговора пока не получен" in missing


def test_legacy_last_utterance_fallback_does_not_claim_complete_dialogue():
    ticket = simple_ticket(utterances=["Мне к конкретному врачу"])
    visible = ReadableHTML(ticket.to_html()).visible
    assert "Полный разговор не сохранён" in visible
    assert "Мне к конкретному врачу" in visible


def test_all_untrusted_fields_remain_escaped_in_both_visible_and_technical_views():
    attack = '<script>alert(1)</script><img src=x onerror="bad()">'
    ticket = simple_ticket(call_id=attack, reason=attack, patient_name=attack, constraints={attack: attack}, utterances=[attack], notes=[attack], highlight_sql=attack, highlight_params={attack: attack}, slots=[{"id": attack, "doctor": attack, "specialty": attack, "starts_at": attack}], machine_view={"query": attack, "parameters": {attack: attack}}, dialogue=[{"role": attack, "text": attack}], user_state={"label": attack, "cues": [attack], "early_intent": attack, "decided_at_ms": attack})
    markup = ticket.to_html()
    parsed = ReadableHTML(markup)
    assert "&lt;script&gt;" in markup
    assert all(tag not in {"script", "img", "iframe"} for tag, _ in parsed.tags)
    assert all(not any(key.startswith("on") for key in attrs) for _, attrs in parsed.tags)
    assert "Помощнику нужна помощь регистратора" in parsed.visible


def test_early_intent_and_state_are_russian_and_bad_milliseconds_do_not_crash():
    ticket = simple_ticket(user_state={"label": "distressed", "cues": ["болит"], "early_intent": "emergency", "decided_at_ms": 600.0})
    visible = ReadableHTML(ticket.to_html()).visible
    assert "Срочное обращение" in visible and "600 мс" in visible
    assert "плохо себя чувствует" in visible and "emergency" not in visible
    ticket.user_state["decided_at_ms"] = "not-a-number"
    assert "плохо себя чувствует" in ReadableHTML(ticket.to_html()).visible


def test_availability_preview_clearly_remains_a_transfer_time_snapshot():
    ticket = simple_ticket(slots=[{"id": 7, "doctor": "Врач", "specialty": "терапевт", "starts_at": "2026-10-21T10:00"}], highlighted_slot_ids=[7])
    markup = ticket.to_html()
    assert "Отдельная выборка из базы на момент передачи" in markup
    assert "Она может отличаться от снимка помощника выше" in markup
    assert "Перед записью проверьте доступность заново" in markup
    assert markup.count('background:#c8f7c5') == 1
    assert "Свободные талоны при передаче звонка" in markup


def test_machine_sql_is_never_executed_during_build_or_render(tmp_path, monkeypatch):
    reg = MockRegistry(str(tmp_path / "never-execute.sqlite"), datetime(2026, 10, 15, 11))
    seed_demo(reg, date(2026, 10, 16))
    destructive = "DELETE FROM patients"
    original_execute = reg.execute
    calls = []
    def execute(sql, params=None):
        calls.append(sql)
        assert sql != destructive
        return original_execute(sql, params)
    monkeypatch.setattr(reg, "execute", execute)
    patient_count = original_execute("SELECT COUNT(*) AS n FROM patients")[0]["n"]
    ticket = build_ticket(reg, "frozen", "requested", {"specialty": "терапевт", "agent_sql": destructive, "agent_sql_params": {}, "agent_seen_slots": []}, [], [])
    ticket.to_html()
    assert destructive not in calls
    assert original_execute("SELECT COUNT(*) AS n FROM patients")[0]["n"] == patient_count
    assert ticket.machine_view["query"] == destructive


def test_audit_timestamps_use_moscow_and_appointment_time_remains_clinic_local():
    from calls.crm.operator import _stamp
    assert _stamp("2026-10-19T05:00:00+00:00", audit_time=True) == "19.10.2026 в 08:00"
    assert _stamp("2026-10-19T05:00:00", audit_time=True) == "19.10.2026 в 08:00"
    assert _stamp("2026-10-19T11:15:00") == "19.10.2026 в 11:15"
