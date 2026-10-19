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
