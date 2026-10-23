"""Offline tests of the Asterisk bridge: AudioSocket framing, DTMF dual-path arbitration, ARI event mapping."""

import socket
import struct
import threading
import time

import numpy as np

from calls.dtmf.synth import dtmf_sequence
from calls.telephony.ari_bridge import AS_AUDIO, AS_HANGUP, AS_UUID, AriBridge


class FakeAri:
    def __init__(self):
        self.log = []

    def answer(self, ch):
        self.log.append(("answer", ch))

    def hangup(self, ch):
        self.log.append(("hangup", ch))

    def external_media(self, app, host_port, call_id):
        self.log.append(("external_media", call_id))
        return {"id": f"media-{call_id}"}

    def bridge(self, *chs):
        self.log.append(("bridge", chs))
        return "bridge-1"


class FakeOrch:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def call(self, op, **args):
        with self.lock:
            self.calls.append((op, args))
        return {"status": "ok", "payload": {}}

    def ops(self, name):
        with self.lock:
            return [a for o, a in self.calls if o == name]


def make_bridge():
    ari, orch = FakeAri(), FakeOrch()
    br = AriBridge(ari, orch, "127.0.0.1:0")
    return br, ari, orch


def test_stasis_start_answers_bridges_media_and_starts_session():
    br, ari, orch = make_bridge()
    br.on_event({"type": "StasisStart", "args": ["inbound"], "channel": {"id": "ch1", "name": "PJSIP/trunk-0001", "caller": {"number": "79035551234"}}})
    assert [l[0] for l in ari.log] == ["answer", "external_media", "bridge"]
    assert orch.calls[0] == ("call.start", {"call_id": "ch1", "flow": "inbound", "caller_phone": "79035551234"})
    # the external media channel entering Stasis must not create a second session
    br.on_event({"type": "StasisStart", "args": [], "channel": {"id": "m1", "name": "UnicastRTP/127.0.0.1-0001"}})
    assert len(orch.ops("call.start")) == 1
    br.on_event({"type": "StasisEnd", "channel": {"id": "ch1"}})
    assert ("hangup", "media-ch1") in ari.log and orch.calls[-1][0] == "call.hangup" and not br.calls


def test_oob_dtmf_is_forwarded_once_and_inband_duplicate_is_suppressed():
    br, ari, orch = make_bridge()
    br.on_event({"type": "StasisStart", "args": ["inbound"], "channel": {"id": "ch1", "name": "PJSIP/x", "caller": {"number": "7"}}})
    ctx = br.calls["ch1"]
    # in-band tone arrives on the media path ...
    pcm, spans = dtmf_sequence("5", on_ms=80, lead_ms=100, trail_ms=400)
    pcm16 = np.clip(pcm, -32768, 32767).astype(np.int16).tobytes()
    oob_at = spans[0][1] + 8 * 40  # Asterisk reports the digit ≈40 ms after the tone ends
    sent = False
    for i in range(0, len(pcm16), 320):
        br._on_audio("ch1", pcm16[i:i + 320])
        if not sent and ctx.samples >= oob_at:
            # ... and the RFC 4733 end event for the same digit: only one call.dtmf must reach the orchestrator
            ctx.wall0 = time.monotonic() - ctx.samples / 8000
            br.on_event({"type": "ChannelDtmfReceived", "digit": "5", "duration_ms": 80, "channel": {"id": "ch1"}})
            sent = True
    digits = orch.ops("call.dtmf")
    assert [d["digit"] for d in digits] == ["5"], digits
    assert digits[0]["source"] == "rfc4733"


def test_inband_fallback_promotes_digit_when_no_oob_arrives():
    br, ari, orch = make_bridge()
    br.on_event({"type": "StasisStart", "args": ["inbound"], "channel": {"id": "ch1", "name": "PJSIP/x", "caller": {"number": "7"}}})
    pcm, _ = dtmf_sequence("42", on_ms=80, off_ms=120, lead_ms=100, trail_ms=400)
    pcm16 = np.clip(pcm, -32768, 32767).astype(np.int16).tobytes()
    for i in range(0, len(pcm16), 320):
        br._on_audio("ch1", pcm16[i:i + 320])
    assert [d["digit"] for d in orch.ops("call.dtmf")] == ["4", "2"]
    assert all(d["source"] == "inband-fallback" for d in orch.ops("call.dtmf"))


def test_audiosocket_framing_roundtrip():
    br, ari, orch = make_bridge()
    br.on_event({"type": "StasisStart", "args": ["inbound"], "channel": {"id": "callA", "name": "PJSIP/x", "caller": {"number": "7"}}})
    port = br.audio.sock.getsockname()[1]
    th = threading.Thread(target=br.audio.serve_forever, daemon=True)
    th.start()
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.sendall(bytes([AS_UUID]) + struct.pack("!H", 5) + b"callA")
    silence = bytes(320)
    for _ in range(10):
        s.sendall(bytes([AS_AUDIO]) + struct.pack("!H", 320) + silence)
    deadline = time.time() + 2
    while br.calls["callA"].samples < 1600 and time.time() < deadline:
        time.sleep(0.01)
    assert br.calls["callA"].samples == 1600
    # playback goes back over the same socket as 20 ms frames
    br.audio.play("callA", bytes(640))
    head = s.recv(3, socket.MSG_WAITALL)
    assert head[0] == AS_AUDIO and struct.unpack("!H", head[1:3])[0] == 320
    body = s.recv(320 + 3 + 320, socket.MSG_WAITALL)
    assert body[320] == AS_AUDIO
    s.sendall(bytes([AS_HANGUP]) + struct.pack("!H", 0))
    deadline = time.time() + 2
    while "callA" in br.audio.conns and time.time() < deadline:
        time.sleep(0.01)
    assert "callA" not in br.audio.conns
    s.close()
