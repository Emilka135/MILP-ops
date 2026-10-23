"""Administrative HTTP graph smoke; real audio acceptance runs separately."""

import json
import time
import urllib.request


def post(base, op, payload):
    request = urllib.request.Request(
        f"{base}/v1/{op}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:  # nosec B310 -- local HTTP endpoint constructed by this launcher
        return json.load(response)["payload"]


def smoke(base):
    stamp = str(time.time_ns())
    call = "smoke-" + stamp
    post(base, "call.start", {"call_id": call, "caller_phone": "79165550000"})
    state = None
    # book -> self -> DOB -> therapist -> accept, on the real compiled Rust graph.
    for digit in "112311199211":
        state = post(base, "call.dtmf", {"call_id": call, "digit": digit})
    assert state["status"] == "booked", state
    assert state["sms"] and "Мария" in state["sms"][0]["text"], state
    bid = state["vars"]["booking_id"]
    rem = "confirm-" + stamp
    post(
        base,
        "call.start",
        {"call_id": rem, "flow": "reminder", "vars": {"booking_id": bid}},
    )
    for digit in "1003#":
        post(base, "call.dtmf", {"call_id": rem, "digit": digit})
    state = post(base, "call.dtmf", {"call_id": rem, "digit": "1"})
    assert state["status"] == "confirmed", state
    rem = "cancel-" + stamp
    post(
        base,
        "call.start",
        {"call_id": rem, "flow": "reminder", "vars": {"booking_id": bid}},
    )
    for digit in "1003#":
        post(base, "call.dtmf", {"call_id": rem, "digit": digit})
    post(base, "call.dtmf", {"call_id": rem, "digit": "0"})
    state = post(base, "call.dtmf", {"call_id": rem, "digit": "1"})
    assert state["status"] == "cancelled", state
    safety = "safety-" + stamp
    post(base, "call.start", {"call_id": safety})
    state = post(
        base,
        "call.speech",
        {"call_id": safety, "text": "да, у меня острая боль в груди"},
    )
    assert state["status"] == "transfer" and state["transfer"]["ticket_id"], state
    return {
        "booking": "passed",
        "confirmation": "passed",
        "cancellation": "passed",
        "medical_handoff": "passed",
        "sms_with_patient_name": "passed",
        "speech": "real TTS; full ASR command acceptance follows",
    }
