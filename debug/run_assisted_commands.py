"""Real assisted-flow acceptance: speech, detected PCM tones, or both.

The deterministic chooser is exercised here, with zero model API calls. Caller
speech reaches call.audio only as PCM; tones reach call.dtmf only after the
production detector and arbiter recognize the generated waveform. CRM snapshots
prove that offers and rejections do not create an appointment. No live MИС or
paid LLM test is implied by this runner.
"""

import argparse
import base64
import json
import math
import subprocess
import sys
import time
import uuid
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/src"), str(ROOT / "debug")]

from calls.dtmf.synth import dtmf_case
from clinic.telephony import Bridge, Context
from run_speech_commands import request


MODES = ("speech-only", "tones-only", "combined")


def save_pcm(path, pcm, sample_rate):
    """Reject silent/invalid recordings before they can stand in for audio."""
    assert isinstance(sample_rate, int) and sample_rate in (8000, 16000, 24000, 48000)
    assert len(pcm) % 2 == 0 and len(pcm) >= sample_rate // 5
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    assert rms > 50, "Audio acceptance requires audible PCM, not silence"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return {"wav": str(path), "sample_rate": sample_rate,
            "audio_s": round(len(samples) / sample_rate, 4), "pcm_rms": round(rms, 2)}


def detect_tone(bridge, context, pcm, expected):
    for offset in range(0, len(pcm), 160):
        bridge.audio(context, pcm[offset:offset + 160].tobytes())
    detected = []
    while not context.events.empty():
        operation, args = context.events.get_nowait()
        assert operation == "call.dtmf", "Tone path must never invoke ASR"
        detected.append(args["digit"])
    assert detected == [expected], (expected, detected)
    return detected[0]


class Acceptance:
    def __init__(self, base, tts_url, output, mode, rate, crm_url="", project="", codec="lin"):
        self.base, self.tts_url = base, tts_url
        self.output, self.mode, self.rate = Path(output), mode, rate
        self.crm_url, self.project, self.codec = crm_url, project, codec
        self.bridge = Bridge(ari=object(), client=object())
        self.contexts = {}
        self.events, self.bot_audio, self.snapshots = [], [], []
        self.prefix = "assisted-" + mode + "-" + uuid.uuid4().hex[:12]
        self.output.mkdir(parents=True, exist_ok=True)

    def post(self, op, **args):
        return request(self.base, op, args)

    def snapshot(self, label):
        if self.crm_url:
            result = request(self.crm_url, "snapshot", {})
        else:
            # CRM is intentionally not published on the host. Read the same
            # HTTP snapshot contract through its existing Compose container.
            command = ["docker", "compose"]
            if self.project:
                command += ["--project-name", self.project]
            code = (
                "import json,urllib.request;"
                "r=urllib.request.Request('http://127.0.0.1:8080/v1/snapshot',"
                "data=b'{}',headers={'Content-Type':'application/json'});"
                "print(json.dumps(json.load(urllib.request.urlopen(r,timeout=15))))"
            )
            command += ["exec", "-T", "crm", "python", "-c", code]
            completed = subprocess.run(command, cwd=ROOT, check=True,
                                       capture_output=True, text=True, timeout=30)
            envelope = json.loads(completed.stdout)
            assert envelope.get("status") == "ok", envelope
            result = envelope["payload"]
        rows = result.get("bookings")
        assert isinstance(rows, list), "CRM snapshot must contain booking rows"
        rows = sorted(rows, key=lambda row: row["id"])
        self.snapshots.append({"label": label, "bookings": rows})
        return rows

    def prompts(self, call, label):
        for index in range(16):
            clip = self.post("call.next_prompt", call_id=call)
            if not clip.get("pcm_b64"):
                return
            pcm = base64.b64decode(clip["pcm_b64"], validate=True)
            path = self.output / "audio" / "bot" / (
                f"{len(self.bot_audio) + 1:03d}-{label}-{index + 1}.wav"
            )
            record = save_pcm(path, pcm, clip["sample_rate"])
            record.update({"call_id": call, "step": label,
                           "audio_source": clip.get("audio_source"),
                           "scenario_source": clip.get("scenario_source"),
                           "audio_key": clip.get("audio_key")})
            self.bot_audio.append(record)
        raise AssertionError("Unbounded bot prompt queue")

    def record(self, call, label, state, event):
        assert not state.get("errors"), state.get("errors")
        context = self.contexts.setdefault(call, Context(call, "assisted-pcm-acceptance"))
        context.listening = bool(state.get("listening"))
        event.update({"call_id": call, "step": label, "state": state})
        self.events.append(event)
        self.prompts(call, label)
        return state

    def say(self, call, label, phrase):
        started = time.perf_counter()
        operation = "speech.synth" if self.tts_url.rstrip("/") == self.base.rstrip("/") else "synth"
        clip = request(self.tts_url, operation, {"text": phrase, "rate": self.rate})
        assert clip.get("engine", "").startswith("silero"), clip.get("engine")
        pcm = base64.b64decode(clip["pcm_b64"], validate=True)
        path = self.output / "audio" / "caller" / f"{len(self.events) + 1:03d}-{label}.wav"
        event = save_pcm(path, pcm, clip["sample_rate"])
        context = self.contexts[call]
        assert context.listening, "The actual scenario must arm speech before caller audio"
        assert clip["sample_rate"] == 8000, "AudioSocket acceptance uses 8-kHz mono PCM"
        # Real caller PCM enters the same detector/VAD as telephony. The trailing
        # pause only marks the end of this audible phrase; it is never its input.
        self.bridge.audio(context, pcm + bytes(8000 * 2))
        pending = []
        while not context.events.empty():
            pending.append(context.events.get_nowait())
        assert len(pending) == 1 and pending[0][0] == "call.audio", [op for op, _ in pending]
        operation, args = pending[0]
        state = self.post(operation, call_id=call, **args)
        assert "ignored" not in state, state
        event.update({"input": "speech", "media_path": "production_detector_vad", "source": phrase,
                      "elapsed_s": round(time.perf_counter() - started, 3)})
        return self.record(call, label, state, event)

    def tone(self, call, label, digit):
        context = self.contexts.setdefault(call, Context(call, "assisted-pcm-acceptance"))
        pcm, _, _ = dtmf_case(digit, 100, codec=self.codec, trail_ms=800)
        path = self.output / "audio" / "tones" / f"{len(self.events) + 1:03d}-{label}.wav"
        event = save_pcm(path, pcm.tobytes(), 8000)
        detected = detect_tone(self.bridge, context, pcm, digit)
        state = self.post("call.dtmf", call_id=call, digit=detected)
        event.update({"input": "detected_pcm_tone", "generated_digit": digit,
                      "detected_digit": detected, "codec": self.codec})
        return self.record(call, label, state, event)

    def start_and_offer(self, suffix):
        call = self.prefix + "-" + suffix
        initial = self.post("call.start", call_id=call, flow="assisted",
                            caller_phone="79165550000", registrar_number="100")
        self.record(call, suffix + "-identify", initial, {"input": "start"})
        assert initial["state"] == "identity.name", initial
        if self.mode == "speech-only":
            state = self.say(call, suffix + "-spoken-name", "Петрова Мария Сергеевна.")
            assert state["state"]=="identity.birth",state
            state = self.say(call, suffix + "-spoken-birth", "Двадцать третье ноября тысяча девятьсот девяносто второго года.")
        else:
            for index, digit in enumerate("21003#23111992", 1):
                state = self.tone(call, suffix + f"-card-{index}", digit)
        assert state["state"] == "assisted.ask_request", state
        if self.mode == "tones-only":
            state = self.tone(call, suffix + "-therapist", "1")
        else:
            state = self.say(call, suffix + "-spoken-specialty", "Запишите меня к терапевту.")
        self.assert_offer(state, 1)
        return call, state

    @staticmethod
    def assert_offer(state, number):
        assert state["state"] == "assisted.offer", state
        variables = state["vars"]
        assert variables["agent_mode"] == "deterministic", "This acceptance is explicitly zero-model-call deterministic mode"
        assert variables["agent_attempt"] == number, variables
        assert variables["agent_completed"] is False and not state["sms"], state
        assert variables.get("booking_id") is None, variables
        assert variables["slot_id"] in {row["id"] for row in variables["agent_seen_slots"]}

    def positive(self):
        before = self.snapshot("positive-before-call")
        call, state = self.start_and_offer("book")
        assert self.snapshot("positive-before-confirmation") == before, "Offer wrote an appointment before consent"
        if self.mode == "tones-only":
            state = self.tone(call, "book-confirm-tone", "1")
        else:
            state = self.say(call, "book-confirm-speech", "Да, подходит.")
        variables = state["vars"]
        assert variables["agent_completed"] is True and variables["booking_status"] == "booked", state
        assert state["sms"] and "Мария" in state["sms"][0]["text"], state
        bid = variables["booking_id"]
        after = self.snapshot("positive-after-confirmation")
        assert bid not in {row["id"] for row in before}
        inserted = [row for row in after if row["id"] == bid]
        assert len(after) == len(before) + 1 and len(inserted) == 1, after
        assert inserted[0]["status"] == "booked" and inserted[0]["slot_id"] == variables["slot_id"]
        if self.mode == "speech-only":
            state = self.say(call, "book-thanks", "Спасибо.")
        else:
            # No injected silence/timeout: let the actual five-second wait end.
            # The success step has no tone 1 edge; ask_more explicitly offers 0.
            deadline = time.monotonic() + 12
            while state["state"] == "assisted.success" and time.monotonic() < deadline:
                time.sleep(0.2)
                state = self.post("call.state", call_id=call)
            assert state["state"] == "assisted.ask_more", state
            self.prompts(call, "book-ask-more")
            state = self.tone(call, "book-finish-tone", "0")
        assert state["finished"] and state["status"] == "booked", state
        return {"call_id": call, "booking_id": bid, "final_state": state,
                "no_booking_before_confirmation": "passed", "sms": "passed"}

    def reject_three(self, operator_available):
        before = self.snapshot("reject-before-call")
        call, state = self.start_and_offer("reject")
        offered = [state["vars"]["slot_id"]]
        for number in range(1, 4):
            assert self.snapshot(f"reject-{number}-before-answer") == before
            last_offer = state["vars"]
            if self.mode == "speech-only":
                state = self.say(call, f"reject-{number}-spoken-no", "Нет.")
            else:
                state = self.tone(call, f"reject-{number}-tone-no", "0")
            if number < 3:
                self.assert_offer(state, number + 1)
                offered.append(state["vars"]["slot_id"])
        assert len(set(offered)) == 3, offered
        assert state["vars"]["agent_attempt"] == 3, state
        assert state["vars"].get("agent_completed") is False and not state["sms"], state
        assert self.snapshot("reject-after-three") == before, "Rejected offers wrote an appointment"
        if operator_available:
            assert state["finished"] and state["status"] == "transfer", state
            ticket = state["transfer"]["ticket"]
            view = ticket["machine_view"]
            assert view["offers_made"] == 3 and view["selection_mode"] == "deterministic", view
            assert view["query"] == last_offer["agent_sql"]
            assert view["parameters"] == last_offer["agent_sql_params"]
            assert view["slots"] == last_offer["agent_seen_slots"]
            assert view["selected_slot_id"] == last_offer["agent_selection"]
            assert ticket["dialogue"], ticket
            route = "operator-with-frozen-machine-view"
        else:
            assert state["state"] == "callback.ask", state
            assert state["vars"]["operator_available"] is False
            assert state["transfer"] is None and not state["finished"], state
            route = "off-hours-callback-question"
        return {"call_id": call, "offered_slot_ids": offered, "offers": 3,
                "route": route, "final_state": state, "no_booking_without_consent": "passed"}


def execute(base, tts_url, output_dir="debug/runs/assisted/speech-only", *, mode="speech-only",
            rate=1.5, operator_available=True, crm_url="", project="", codec="lin"):
    assert mode in MODES
    runner = Acceptance(base, tts_url, output_dir, mode, rate, crm_url, project, codec)
    try:
        booked = runner.positive()
        rejected = runner.reject_three(operator_available)
        speech_count = sum(event.get("input") == "speech" for event in runner.events)
        tone_count = sum(event.get("input") == "detected_pcm_tone" for event in runner.events)
        if mode == "speech-only":
            assert speech_count > 0 and tone_count == 0
        elif mode == "tones-only":
            assert tone_count > 0 and speech_count == 0
        else:
            assert tone_count > 0 and speech_count > 0
        report = {"status": "passed", "mode": mode, "agent_mode": "deterministic",
                  "model_calls": 0, "asr_inputs": speech_count, "dtmf_inputs": tone_count,
                  "operator_available_expected": operator_available, "positive": booked,
                  "three_rejections": rejected, "events": runner.events,
                  "bot_audio": runner.bot_audio, "crm_snapshots": runner.snapshots,
                  "scope": "Local deterministic assisted flow; no paid LLM or real clinic booking verification"}
        (runner.output / "commands.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report
    except Exception as error:
        (runner.output / "failed-progress.json").write_text(json.dumps({
            "status": "failed", "mode": mode, "error_type": type(error).__name__,
            "events": runner.events, "bot_audio": runner.bot_audio,
            "crm_snapshots": runner.snapshots,
        }, ensure_ascii=False, indent=2) + "\n")
        raise


def self_test(output):
    """Actual offline tone detection only; never reported as API acceptance."""
    folder = Path(output)
    bridge = Bridge(ari=object(), client=object())
    records = []
    for codec in ("lin", "alaw", "ulaw"):
        context = Context("self-test-" + codec, "offline", listening=True)
        for index, digit in enumerate("123A456B789C*0#D", 1):
            pcm, _, _ = dtmf_case(digit, 100, codec=codec, trail_ms=800)
            detected = detect_tone(bridge, context, pcm, digit)
            record = save_pcm(folder / "audio" / f"{codec}-{index:02d}.wav", pcm.tobytes(), 8000)
            record.update({"codec": codec, "generated": digit, "detected": detected})
            records.append(record)
    report = {"status": "passed", "scope": "Offline production DTMF detector only, no API/ASR/LLM calls",
              "cases": len(records), "tones": records}
    (folder / "self-test.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", default="http://127.0.0.1:8080")
    parser.add_argument("--tts-url", default="")
    parser.add_argument("--crm-url", default="", help="Optional directly reachable CRM; default uses Compose exec read-only snapshot")
    parser.add_argument("--compose-project", default="")
    parser.add_argument("--mode", choices=MODES, default="speech-only")
    parser.add_argument("--rate", type=float, default=1.5)
    parser.add_argument("--codec", choices=("lin", "alaw", "ulaw"), default="lin")
    parser.add_argument("--operator-unavailable", action="store_true")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--self-test", action="store_true", help="Offline real PCM detector check, not assisted-flow acceptance")
    args = parser.parse_args()
    output = args.output_dir or f"debug/runs/assisted/{args.mode}"
    if args.self_test:
        report = self_test(output)
        print(json.dumps({key: value for key, value in report.items() if key != "tones"}, ensure_ascii=False))
    else:
        report = execute(args.orchestrator, args.tts_url or args.orchestrator, output,
                         mode=args.mode, rate=args.rate, operator_available=not args.operator_unavailable,
                         crm_url=args.crm_url, project=args.compose_project, codec=args.codec)
        print(json.dumps({key: report[key] for key in ("status", "mode", "agent_mode", "model_calls", "asr_inputs", "dtmf_inputs")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
