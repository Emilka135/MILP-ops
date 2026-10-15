import struct
import threading
import uuid

from clinic.telephony import Bridge, Context


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
