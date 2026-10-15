"""Real speech: offline operator asks when to call back; emergency bypasses deferral."""

import argparse
import base64
import json
import uuid
import wave
from pathlib import Path

from run_speech_commands import request


def execute(base, tts_url, folder="debug/runs/callback"):
    folder = Path(folder)
    (folder / "audio").mkdir(parents=True, exist_ok=True)
    records = []

    def say(call, phrase):
        clip = request(tts_url, "synth", {"text": phrase, "rate": 1.5})
        assert clip["engine"].startswith("silero")
        path = folder / "audio" / f"{len(records) + 1:02d}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(clip["sample_rate"])
            wav.writeframes(base64.b64decode(clip["pcm_b64"]))
        result = request(
            base,
            "call.audio",
            {"call_id": call, "pcm_b64": clip["pcm_b64"], "rate": clip["sample_rate"]},
        )
        records.append(
            {
                "phrase": phrase,
                "state": result["state"],
                "status": result["status"],
                "wav": str(path),
            }
        )
        return result

    call = "callback-" + uuid.uuid4().hex
    request(
        base,
        "call.start",
        {"call_id": call, "flow": "voice", "caller_phone": "79165550000"},
    )
    result = say(call, "Позовите оператора.")
    assert result["state"] == "callback.ask" and result["transfer"] is None, result
    result = say(call, "Завтра в пятнадцать часов.")
    assert result["status"] == "callback_requested" and result["transfer"] is None, (
        result
    )
    # Emergency is also accepted before identity and never queued overnight.
    call = "emergency-" + uuid.uuid4().hex
    request(base, "call.start", {"call_id": call, "flow": "voice"})
    result = say(call, "У меня острая боль в груди.")
    assert result["status"] == "transfer", result
    report = {
        "mode": "real-speech-offline-operator",
        "results": {
            "ask_when": "passed",
            "save_callback": "passed",
            "no_overnight_transfer": "passed",
            "emergency_immediate": "passed",
        },
        "utterances": records,
    }
    (folder / "commands.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--orchestrator", default="http://127.0.0.1:28280")
    p.add_argument("--tts-url", default="http://127.0.0.1:28285")
    ns = p.parse_args()
    print(
        json.dumps(execute(ns.orchestrator, ns.tts_url), ensure_ascii=False, indent=2)
    )
