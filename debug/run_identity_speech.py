"""Real Silero -> CUDA ASR -> exact identity lookup on synthetic patients.

Also exercises a historical spoken birthday through the running Rust scenario.
Expected text is used only by assertions, never sent to ASR. Failed first attempts
and explicit full-card recovery remain in the report, alongside audible WAVs.
"""

import argparse
import base64
from datetime import date, datetime
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/src")]
from calls.crm.registry import MockRegistry
from clinic.identity import (
    ALIAS_SCHEMA, IdentityLookup, checked_pronunciation,
    normalize_birth_date, normalize_identifier,
)
from run_assisted_commands import Acceptance, save_pcm
from run_speech_commands import request

NAMES = [
    ("Шварценеггер", "Шварцен+еггер"),
    ("Хачатурян", "Хачатур+ян"),
    ("O’Connor", "О К+оннор"),
    ("Петрова-Смирнова", "Петр+ова-Смирн+ова"),
]


class Corpus:
    def __init__(self, base, output, provider):
        self.base, self.output, self.provider = base, Path(output), provider
        self.output.mkdir(parents=True, exist_ok=True)
        self.reg = MockRegistry(str(self.output / ("synthetic-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite")))
        self.reg._conn.executescript(ALIAS_SCHEMA)
        for number, (surname, _) in enumerate(NAMES, 1):
            self.reg.add_patient(surname, "Синтетический", "Пациент", date(1990, 5, 5),
                                 str(11000000000 + number), f"0100{number+2}", str(79160000000 + number))
        for alias in ("О’Коннор", "О Коннор", "Оконнор"):
            self.reg._x("INSERT INTO identity_name_aliases VALUES(3,?,1,?)",
                        (alias, "explicit synthetic test pronunciation"))
        self.lookup = IdentityLookup(self.reg)

    def recognize(self, label, source):
        clip = request(self.base, "speech.synth", {"text": source, "rate": 1.3})
        pcm = base64.b64decode(clip["pcm_b64"], validate=True)
        audio = save_pcm(self.output / "audio" / (label + ".wav"), pcm, clip["sample_rate"])
        # This internal request contains ONLY audio and its sample rate.
        code = (
            "import sys,urllib.request;"
            "req=urllib.request.Request('http://127.0.0.1:8080/v1/transcribe',"
            "data=sys.stdin.buffer.read(),headers={'Content-Type':'application/json'});"
            "print(urllib.request.urlopen(req,timeout=60).read().decode())"
        )
        completed = subprocess.run(
            ["docker", "compose", "exec", "-T", "asr", "python", "-c", code],
            input=json.dumps({"pcm_b64": clip["pcm_b64"], "rate": clip["sample_rate"]}),
            text=True, capture_output=True, check=True, timeout=90,
        )
        envelope = json.loads(completed.stdout)
        assert envelope["status"] == "ok", envelope
        asr = envelope["payload"]
        assert asr["provider"] == self.provider, asr
        if self.provider == "CUDAExecutionProvider":
            assert asr["cuda_verified"] is True, asr
        return {**audio, "asr": asr}

    def surname(self, label, source, expected):
        record = self.recognize(label, source)
        status, result = self.lookup.lookup({"surname": record["asr"]["text"], "card_no": f"0100{expected+2}"})
        record.update(lookup_status=status, lookup_result=result, first_attempt_name_recognized=status == "ok")
        assert status != "ok" or result["patient_id"] == expected, "ASR selected another patient"
        if status != "ok":
            # Explicitly switch methods. Never turn a guessed surname into an
            # approved alias or silently discard the mismatch on the same lookup.
            digits = f"0100{expected+2}"
            words = ("Ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
            spoken = ". ".join(words[int(digit)] for digit in digits) + "."
            recovery = self.recognize(label + "-repeat-full-card", spoken)
            number = normalize_identifier(recovery["asr"]["text"])
            recovery_status, patient = self.lookup.lookup({"card_no": number})
            assert recovery_status == "ok" and patient["patient_id"] == expected
            record.update(clarification=recovery, recovery="explicit_full_card_after_unrecognized_surname")
        return record

    def birthday(self):
        record = self.recognize("birth-1990", "Пятого мая тысяча девятьсот девяностого года.")
        normalized = normalize_birth_date(record["asr"]["text"], today=date(2026, 10, 19)).isoformat()
        assert normalized == "1990-05-05"
        return {**record, "normalized": normalized}

    def card(self):
        record = self.recognize("card-leading-zero", "Ноль. Один. Ноль. Ноль. Три.")
        normalized = normalize_identifier(record["asr"]["text"])
        assert normalized == "01003"
        status, patient = self.lookup.lookup({"card_no": normalized})
        assert status == "ok" and patient["patient_id"] == 1
        return {**record, "normalized": normalized}

    def rust_birthday(self):
        runner = Acceptance(self.base, self.base, self.output / "rust-birth", "speech-only", 1.3)
        before = runner.snapshot("before-identity")
        call = "identity-dob-" + uuid.uuid4().hex
        state = runner.post("call.start", call_id=call, caller_phone="79165550000")
        runner.record(call, "start", state, {"input": "start"})
        runner.tone(call, "select-booking", "1")
        state = runner.tone(call, "self-identity", "1")
        assert state["state"] == "inbound.id_birth_check" and state["listening"] is True, state
        state = runner.say(call, "spoken-historical-birthday",
                           "Двадцать третьего ноября тысяча девятьсот девяносто второго года.")
        assert state["state"] == "inbound.book_start" and state["vars"]["patient_id"] == 3, state
        assert "said_date" not in state["vars"] and not state["sms"], state
        assert runner.snapshot("after-identity") == before
        return {"namespace": "running_local_demo_clinic", "events": runner.events,
                "bot_audio": runner.bot_audio, "patient_id": 3, "appointment_mutations": 0}

    def run(self):
        cases = [(f"surname-{i}", lambda i=i, s=s, p=p: self.surname(f"surname-{i}", checked_pronunciation(s, p), i))
                 for i, (s, p) in enumerate(NAMES, 1)]
        cases += [("birth-1990", self.birthday), ("card-leading-zero", self.card),
                  ("rust-spoken-birth-date", self.rust_birthday)]
        records = []
        for label, execute in cases:
            record = {"case": label}
            try:
                record.update(execute(), status="passed")
            except Exception as error:
                record.update(status="needs_review", error_type=type(error).__name__)
            records.append(record)
            report = {"status": "running", "expected_case_count": len(cases),
                      "scope": "Synthetic identities only; exact/approved-alias lookup, no automatic fuzzy match",
                      "cases": records, "appointment_mutations": self.reg.execute("SELECT count(*) AS n FROM bookings")[0]["n"]}
            if len(records) == len(cases):
                report["status"] = "passed" if all(row["status"] == "passed" for row in records) else "needs_review"
            (self.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="debug/runs/identity-module/current")
    parser.add_argument("--orchestrator", default="http://127.0.0.1:8080")
    parser.add_argument("--expected-provider", default="CUDAExecutionProvider")
    args = parser.parse_args()
    report = Corpus(args.orchestrator, args.output, args.expected_provider).run()
    print(json.dumps({"status": report["status"], "passed": sum(r["status"] == "passed" for r in report["cases"]),
                      "total": len(report["cases"])}))
    return report["status"] != "passed"


if __name__ == "__main__":
    raise SystemExit(main())
