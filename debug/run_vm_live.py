"""Explicit live acceptance on the single synthetic imported card 1001.

Creates one test visit per mode and cancels only that newly created visit.
Actual Silero PCM enters the production VAD/detector and local GigaAM ASR.
Run from the project Docker network; keep all evidence under debug/runs/.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/src")]
from clinic.telephony import Bridge, Context
from calls.dtmf.synth import dtmf_case


def post(base, op, data):
    headers = {"Content-Type": "application/json"}
    if base.endswith("orchestrator:8080"):
        headers["Authorization"] = "Bearer " + os.environ["API_TOKEN"]
    request = urllib.request.Request(base + "/v1/" + op, data=json.dumps(data).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=190) as response:
        envelope = json.load(response)
    if envelope.get("status") != "ok":
        raise RuntimeError({"operation": op, "reply": envelope})
    return envelope["payload"]


def run(mode, output):
    folder = output / mode
    folder.mkdir(parents=True, exist_ok=True)
    base = "http://orchestrator:8080"
    call_id = "vm-live-" + mode + "-" + uuid.uuid4().hex[:12]
    events = []
    bridge = Bridge(ari=object(), client=object())
    context = Context(call_id, "live-acceptance")

    def record(label, state, **extra):
        context.listening = bool(state.get("listening"))
        events.append({"step": label, "state": state, **extra})
        (folder / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
        print(json.dumps({"step": label, "state": state.get("state"), "status": state.get("status"), "asr": state.get("vars", {}).get("last_utterance")}, ensure_ascii=False), flush=True)
        return state

    def tone(digit):
        pcm, _, _ = dtmf_case(digit, 100, codec="alaw", trail_ms=800)
        for offset in range(0, len(pcm), 160):
            bridge.audio(context, pcm[offset:offset + 160].tobytes())
        operation, args = context.events.get_nowait()
        assert operation == "call.dtmf" and args["digit"] == digit
        assert context.events.empty()
        return record("tone-" + digit, post(base, operation, {"call_id": call_id, **args}))

    def say(label, text):
        clip = post("http://tts:8080", "synth", {"text": text, "rate": 1.3})
        pcm = base64.b64decode(clip["pcm_b64"])
        assert clip["sample_rate"] == 8000 and any(pcm)
        with wave.open(str(folder / (label + ".wav")), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(pcm)
        assert context.listening
        bridge.audio(context, pcm + bytes(16000))
        operation, args = context.events.get_nowait()
        assert operation == "call.audio" and context.events.empty()
        return record(label, post(base, operation, {"call_id": call_id, **args}), source=text, media_path="production_detector_vad")

    state = record("start", post(base, "call.start", {"call_id": call_id, "flow": "assisted", "registrar_number": "114"}))
    if mode == "speech-only":
        state = say("name", "Иванов Иван Иванович.")
        state = say("birth", "Первое января тысяча девятьсот девяностого года.")
    else:
        for digit in "21001#01011990":
            state = tone(digit)
    assert state["state"] == "assisted.ask_request", state
    patient_id = state['vars']['patient_id']
    state = tone("1") if mode == "tones-only" else say("specialty", "Запишите меня к терапевту.")
    assert state["state"] == "assisted.offer", state
    assert not state["vars"].get("booking_id"), "Offer must not create a visit"
    assert state["vars"]["agent_mode"] == "openai", state
    state = tone("1") if mode == "tones-only" else say("consent", "Да, подходит.")
    assert state["vars"].get("booking_status") == "booked", state
    booking_id = state["vars"]["booking_id"]
    result = {"status": "passed", "mode": mode, "call_id": call_id, "booking_id": booking_id,
              "agent_mode": state["vars"].get("agent_mode"), "model_call_verified": True,
              "scope": "real local speech / production VAD / Rust / real gateway / DBI; synthetic patient only"}
    post(base, "call.hangup", {"call_id": call_id})
    if mode == 'tones-only':
        target = next(slot for slot in state['vars']['agent_seen_slots'] if slot['id'] != state['vars']['slot_id'])
        moved = post('http://crm:8080', 'reschedule', {'booking_id':booking_id, 'patient_id':patient_id,
                     'slot_id':target['id'], 'operation_key':call_id+'-move'})
        assert moved.get('stand_acknowledged') is True, moved
        result['reschedule'] = moved
    cleanup = post("http://crm:8080", "cancel", {"booking_id": booking_id, "patient_id": patient_id, "operation_key": call_id + "-cleanup"})
    result["cleanup"] = cleanup
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["speech-only", "tones-only", "combined"], required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "debug/runs/vm-deployment/live")
    args = parser.parse_args()
    run(args.mode, args.output)
