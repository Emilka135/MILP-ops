"""SMS after the call: templates with the booked person's name (mandatory) + a verbal repeat.

The voice prompt already repeats the date/time; the SMS is the written copy
the patient keeps.  ``render`` refuses to produce a message without the
patient's name, so a template regression cannot ship anonymous messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional, Protocol

TEMPLATES: dict[str, str] = {
    "booked": "{patient_name}, вы записаны к {specialty_dative}: {slot_date_h} в {slot_time}, каб. {cabinet}, врач {doctor}. {clinic_name}. Возьмите паспорт и полис. Отмена/перенос: {clinic_phone}. Мы не просим коды из СМС.",
    "rescheduled": "{patient_name}, запись к {specialty_dative} перенесена: {slot_date_h} в {slot_time}, каб. {cabinet}, врач {doctor}. {clinic_name}, {clinic_phone}.",
    "cancelled": "{patient_name}, запись к {specialty_dative} {slot_date_h} в {slot_time} отменена. Новая запись: {clinic_phone}. {clinic_name}.",
    "confirmed": "{patient_name}, запись к {specialty_dative} {slot_date_h} в {slot_time}, каб. {cabinet} подтверждена. Ждём вас! {clinic_name}.",
    "reminder": "{patient_name}, напоминаем о приёме у {specialty_dative} {slot_date_h} в {slot_time}, каб. {cabinet}. Подтвердить/отменить: {clinic_phone}. {clinic_name}.",
}
_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class SmsError(ValueError):
    pass


def human_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month]}"


def render(template: str, vars: dict, defaults: Optional[dict] = None) -> str:
    if template not in TEMPLATES:
        raise SmsError(f"unknown SMS template {template!r}")
    data = {"clinic_name": "Поликлиника", "clinic_phone": "", **(defaults or {}), **{k: v for k, v in vars.items() if v is not None}}
    name = str(data.get("patient_name") or "").strip()
    if not name:
        raise SmsError("SMS must contain the booked person's name (patient_name is empty)")
    if "slot_date" in data and "slot_date_h" not in data:
        data["slot_date_h"] = human_date(str(data["slot_date"]))
    text = TEMPLATES[template]
    missing = [k for k in _PLACEHOLDER.findall(text) if k not in data]
    if missing:
        raise SmsError(f"SMS template {template!r} misses variables: {missing}")
    out = text.format(**data)
    return re.sub(r"\s{2,}", " ", out).strip()


class SmsGateway(Protocol):
    def send(self, phone: str, text: str) -> str: ...


@dataclass
class MockSmsGateway:
    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, phone: str, text: str) -> str:
        self.sent.append((phone, text))
        return f"mock-{len(self.sent)}"


@dataclass
class SmsModule:
    gateway: SmsGateway
    phone_lookup: Callable[[dict], Optional[str]]  # vars → phone (resolved from patient_id, never from prompts)
    defaults: dict = field(default_factory=dict)

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op != "send":
            return "error", {"error": f"unknown op {op}"}
        vars = args.get("vars", {})
        try:
            text = render(args["template"], vars, self.defaults)
        except SmsError as exc:
            return "error", {"error": str(exc)}
        phone = self.phone_lookup(vars)
        if not phone:
            return "error", {"error": "no phone for patient", "text": text}
        msg_id = self.gateway.send(phone, text)
        return "ok", {"message_id": msg_id, "text": text, "chars": len(text)}
