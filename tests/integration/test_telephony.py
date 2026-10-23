import struct
import threading
import uuid

from clinic.telephony import Bridge, Context


def test_ari_keeps_subscription_across_idle_longer_than_connect_timeout():
    import socket
    import time
    from calls.telephony.ari_bridge import WsClient

    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        with server:
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\n\r\n')
                time.sleep(0.15)
                connection.sendall(b'\x81\x02{}')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = WsClient(f'ws://127.0.0.1:{port}/ari/events?app=calls', timeout=0.05)
    try:
        assert client.recv_text() == '{}'
    finally:
        client.sock.close()
        thread.join(timeout=2)


class Connection:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def recv(self, length):
        part, self.data = self.data[: min(length, 2)], self.data[min(length, 2) :]
        return part

    def settimeout(self, _):
        pass

    def close(self):
        self.closed = True


def packet(kind, data):
    return struct.pack("!BH", kind, len(data)) + data


def test_audio_socket_accepts_binary_uuid_and_routes_fragmented_pcm():
    bridge = Bridge(ari=object(), client=object())
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for call in ids:
        bridge.calls[call] = Context(call, call)
    frames = []
    bridge.audio = lambda ctx, pcm: frames.append((ctx.call_id, pcm))
    for call in reversed(ids):
        conn = Connection(
            packet(1, uuid.UUID(call).bytes)
            + packet(0x10, b"\x01\x02" * 160)
            + packet(0, b"")
        )
        bridge.media_connection(conn)
        assert conn.closed
    assert [x[0] for x in frames] == list(reversed(ids))
    assert all(x[1] == b"\x01\x02" * 160 for x in frames)


def test_audio_socket_rejects_text_uuid_without_routing_audio():
    bridge = Bridge(ari=object(), client=object())
    call = str(uuid.uuid4())
    bridge.calls[call] = Context(call, call)
    frames = []
    bridge.audio = lambda *args: frames.append(args)
    conn = Connection(packet(1, call.encode()) + packet(0x10, b"\x01\x02"))
    bridge.media_connection(conn)
    assert conn.closed and not frames


def test_playback_preserves_prompt_until_media_connection_exists():
    class Client:
        def call(self, *args, **kwargs):
            raise AssertionError("prompt consumed before AudioSocket handshake")

    bridge = Bridge(ari=object(), client=Client())
    ctx = Context(str(uuid.uuid4()), "waiting-media")
    worker = threading.Thread(target=bridge.playback, args=(ctx,))
    worker.start()
    ctx.stop.set()
    worker.join(timeout=1)
    assert not worker.is_alive()


import numpy as np
import pytest
from calls.dtmf.synth import dtmf_case


@pytest.mark.parametrize("codec", ["lin", "alaw", "ulaw"])
@pytest.mark.parametrize("duration_ms", [100, 300])
@pytest.mark.parametrize("digit", list("123A456B789C*0#D"))
def test_tones_do_not_become_asr_when_scenario_is_listening(codec, duration_ms, digit):
    bridge = Bridge(ari=object(), client=object())
    ctx = Context("armed-tone-test", "test", listening=True)
    pcm, _, _ = dtmf_case(digit, duration_ms, codec=codec, trail_ms=800)
    # Exercise an AudioSocket read larger than a single 20-ms frame as well.
    bridge.audio(ctx, pcm.tobytes())
    assert list(ctx.events.queue) == [("call.dtmf", {"digit": digit})]
    assert not ctx.voiced and not ctx.utterance and not ctx.last_utterance


def test_key_interrupts_stale_speech_and_preserves_next_spoken_pcm():
    bridge = Bridge(ari=object(), client=object())
    ctx = Context("speech-tone-speech", "test", listening=True)
    # A fixed broadband fragment exercises buffering, not speech recognition.
    speech = np.random.default_rng(42).integers(-2000, 2001, 3200, dtype=np.int16)
    bridge.audio(ctx, speech.tobytes())
    assert ctx.voiced
    pcm, _, _ = dtmf_case("2", 100, trail_ms=800)
    bridge.audio(ctx, pcm.tobytes())
    assert list(ctx.events.queue) == [("call.dtmf", {"digit": "2"})]
    ctx.events.get_nowait()
    bridge.audio(ctx, speech.tobytes() + np.zeros(6400, dtype=np.int16).tobytes())
    [(operation, args)] = list(ctx.events.queue)
    assert operation == "call.audio" and args["rate"] == 8000
    import base64
    heard = base64.b64decode(args["pcm_b64"])
    assert heard.startswith(speech.tobytes())


def test_key_still_interrupts_bot_playback_without_arming_asr():
    bridge = Bridge(ari=object(), client=object())
    ctx = Context("tone-barge-in", "test", playing=True, listening=True)
    pcm, _, _ = dtmf_case("*", 100, trail_ms=800)
    bridge.audio(ctx, pcm.tobytes())
    assert list(ctx.events.queue) == [("call.dtmf", {"digit": "*"})]
    assert ctx.generation == 1
