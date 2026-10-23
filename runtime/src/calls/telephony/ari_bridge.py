"""Asterisk ARI ↔ orchestrator bridge (skeleton that runs with stdlib only).

Responsibilities (see docs/ARCHITECTURE.md §telephony):
  * ARI WebSocket  /ari/events?app=calls      → StasisStart / ChannelDtmfReceived / StasisEnd
  * AudioSocket    TCP server (16-bit 8 kHz)  → per-call PCM frames for the in-band DTMF fallback, VAD and ASR
  * orchestrator   POST /v1/call.start | call.dtmf | call.speech | call.partial | call.audio | call.silence | call.prompt_done | call.hangup
                   (call.partial = streaming ASR partials for the early intent / caller-state tracker; the bridge
                   sends audio, the ASR module – or a Deeray WS client – produces the partials)
  * playback       orchestrator says what to play (TTS pcm) → AudioSocket write; `stop_playback` → flush

Only the control plane is wired here: ARI DTMF events are primary, the in-band detector
(``calls.dtmf``) runs on the AudioSocket stream and the arbiter de-duplicates within 100 ms.  Streaming ASR
is attached per call when the orchestrator arms the ``listen`` action (60 s without keypress or early speech).

This module is intentionally dependency-free; the WebSocket client is a tiny RFC 6455 implementation
sufficient for ARI's text frames.  Run:  ARI_URL=http://pbx:8088 ARI_USER=calls ARI_PASSWORD=… python -m calls.telephony.ari_bridge
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from calls.dtmf.arbiter import DtmfArbiter
from calls.dtmf.events import DigitEnd
from calls.dtmf.inband import InbandDtmfDetector
from calls.services.netutil import open_http, post_json


# ---- tiny WebSocket client (text frames only) --------------------------------------------------------------


class WsClient:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path}?{u.query} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("ARI websocket handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        # The connection/handshake deadline is not an idle-call deadline.
        # ARI can be silent between calls; closing it every 30 s detaches Stasis.
        self.sock.settimeout(None)

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ARI websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self) -> Optional[str]:
        while True:
            b1, b2 = self._read(2)
            opcode, ln = b1 & 0x0F, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(ln)
            if opcode == 0x8:
                return None
            if opcode == 0x9:  # ping → pong
                self._send(0xA, payload)
                continue
            if opcode == 0x1:
                return payload.decode()

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        head = bytes([0x80 | opcode])
        if len(payload) < 126:
            head += bytes([0x80 | len(payload)])
        elif len(payload) < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", len(payload))
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", len(payload))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


# ---- ARI REST helpers ----------------------------------------------------------------------------------------


@dataclass
class Ari:
    url: str
    user: str
    password: str

    def _req(self, method: str, path: str, **params: object) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{self.url}/ari{path}?{q}", method=method)
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        with open_http(req, timeout=10) as r:
            body = r.read()
            return json.loads(body) if body else {}

    def answer(self, channel: str) -> None:
        self._req("POST", f"/channels/{channel}/answer")

    def hangup(self, channel: str) -> None:
        try:
            self._req("DELETE", f"/channels/{channel}")
        except Exception as exc:  # channel may already be gone
            print(f"[ari] hangup {channel}: {exc}", flush=True)

    def external_media(self, app: str, host_port: str, call_id: str) -> dict:
        # AudioSocket transport: Asterisk connects to us and streams 8 kHz slin both ways
        return self._req("POST", "/channels/externalMedia", app=app, external_host=host_port, format="slin", transport="tcp",
                         encapsulation="audiosocket", data=call_id)

    def bridge(self, *channels: str) -> str:
        b = self._req("POST", "/bridges", type="mixing")
        self._req("POST", f"/bridges/{b['id']}/addChannel", channel=",".join(channels))
        return str(b["id"])

    def originate(self, endpoint: str, app: str, args: str, caller_id: str, timeout: int = 45) -> dict:
        return self._req("POST", "/channels", endpoint=endpoint, app=app, appArgs=args, callerId=caller_id, timeout=timeout)


# ---- orchestrator client ----------------------------------------------------------------------------------


@dataclass
class OrchestratorClient:
    url: str

    def call(self, op: str, **args: object) -> dict:
        return post_json(f"{self.url}/v1/{op}", dict(args), timeout=10)


# ---- AudioSocket server -----------------------------------------------------------------------------------------

AS_HANGUP, AS_UUID, AS_AUDIO, AS_ERROR = 0x00, 0x01, 0x10, 0xFF


class AudioSocketServer:
    """Accepts Asterisk AudioSocket connections; hands 20 ms PCM16 frames to ``on_audio(call_id, pcm)``."""

    def __init__(self, bind: str, on_audio: Callable[[str, bytes], None], on_close: Callable[[str], None]) -> None:
        host, port = bind.rsplit(":", 1)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, int(port)))
        self.sock.listen(64)
        self.on_audio, self.on_close = on_audio, on_close
        self.conns: dict[str, socket.socket] = {}
        self._lock = threading.Lock()

    def serve_forever(self) -> None:
        while True:
            conn, _ = self.sock.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        call_id = ""
        try:
            while True:
                head = conn.recv(3, socket.MSG_WAITALL)
                if len(head) < 3:
                    break
                kind, ln = head[0], struct.unpack("!H", head[1:3])[0]
                payload = conn.recv(ln, socket.MSG_WAITALL) if ln else b""
                if kind == AS_UUID:
                    call_id = payload.decode(errors="replace")  # Asterisk sends the dialplan `data` (our call id)
                    with self._lock:
                        self.conns[call_id] = conn
                elif kind == AS_AUDIO and call_id:
                    self.on_audio(call_id, payload)
                elif kind in (AS_HANGUP, AS_ERROR):
                    break
        finally:
            if call_id:
                with self._lock:
                    self.conns.pop(call_id, None)
                self.on_close(call_id)
            conn.close()

    def play(self, call_id: str, pcm: bytes, frame_bytes: int = 320) -> None:
        """Send PCM16 8 kHz in 20 ms frames, paced in real time (Asterisk expects a live stream)."""
        conn = self.conns.get(call_id)
        if conn is None:
            return
        t0 = time.monotonic()
        for i in range(0, len(pcm), frame_bytes):
            frame = pcm[i:i + frame_bytes]
            conn.sendall(bytes([AS_AUDIO]) + struct.pack("!H", len(frame)) + frame)
            sleep_until = t0 + (i // frame_bytes + 1) * 0.02
            time.sleep(max(0.0, sleep_until - time.monotonic()))


# ---- bridge ---------------------------------------------------------------------------------------------------


@dataclass
class CallCtx:
    """Per-call media state.  All DTMF timing is on the media clock (samples @ 8 kHz), as in the arbiter tests."""

    call_id: str
    channel: str
    media_channel: str = ""
    arbiter: DtmfArbiter = field(default_factory=lambda: DtmfArbiter(oob_negotiated=True, inband_fallback=True))
    detector: InbandDtmfDetector = field(default_factory=InbandDtmfDetector)
    samples: int = 0  # media clock
    wall0: float = field(default_factory=time.monotonic)
    speech_buf: bytearray = field(default_factory=bytearray)

    def now(self) -> int:
        """Media-clock estimate for an out-of-band event arriving on the wall clock."""
        return max(self.samples, int((time.monotonic() - self.wall0) * 8000))


class AriBridge:
    def __init__(self, ari: Ari, orch: OrchestratorClient, audiosocket_bind: str, app: str = "calls") -> None:
        self.ari, self.orch, self.app = ari, orch, app
        self.calls: dict[str, CallCtx] = {}
        self.by_channel: dict[str, str] = {}
        self.audio = AudioSocketServer(audiosocket_bind, self._on_audio, self._on_media_closed)
        self.media_host = os.environ.get("AUDIOSOCKET_ADVERTISE", audiosocket_bind)

    # events ----------------------------------------------------------------------------------------------
    def run(self) -> None:
        threading.Thread(target=self.audio.serve_forever, daemon=True).start()
        threading.Thread(target=self._poll_prompts, daemon=True).start()
        u = urllib.parse.urlsplit(self.ari.url)
        ws_url = f"ws://{u.hostname}:{u.port or 8088}/ari/events?app={self.app}&api_key={self.ari.user}:{self.ari.password}&subscribeAll=false"
        while True:
            try:
                ws = WsClient(ws_url)
                print("[ari] connected", flush=True)
                while True:
                    msg = ws.recv_text()
                    if msg is None:
                        break
                    self.on_event(json.loads(msg))
            except Exception as exc:
                print(f"[ari] {exc}; reconnecting in 2 s", flush=True)
                time.sleep(2)

    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        ch = ev.get("channel", {})
        if kind == "StasisStart":
            args = ev.get("args", [])
            if ch.get("name", "").startswith("UnicastRTP") or ch.get("name", "").startswith("AudioSocket"):
                return  # our own external-media channel entering Stasis
            flow = args[0] if args else "inbound"
            call_id = ch["id"]
            caller = ch.get("caller", {}).get("number", "") if flow == "inbound" else (args[1] if len(args) > 1 else "")
            ctx = CallCtx(call_id, ch["id"])
            self.calls[call_id] = ctx
            self.by_channel[ch["id"]] = call_id
            self.ari.answer(ch["id"])
            media = self.ari.external_media(self.app, self.media_host, call_id)
            ctx.media_channel = media.get("id", "")
            self.ari.bridge(ch["id"], ctx.media_channel)
            self.orch.call("call.start", call_id=call_id, flow=flow, caller_phone=caller)
        elif kind == "ChannelDtmfReceived":
            call_id = self.by_channel.get(ch.get("id", ""))
            if call_id:
                digit = ev.get("digit", "")
                ctx = self.calls[call_id]
                t_end = ctx.now()
                t_start = t_end - int(float(ev.get("duration_ms", 80)) * 8)  # ARI reports the digit at its end
                for accepted in ctx.arbiter.on_oob(digit, t_start, t_end):
                    self.orch.call("call.dtmf", call_id=call_id, digit=accepted.digit, source="rfc4733")
        elif kind == "StasisEnd":
            call_id = self.by_channel.pop(ch.get("id", ""), None)
            if call_id and call_id in self.calls:
                ctx = self.calls.pop(call_id)
                if ctx.media_channel:
                    self.ari.hangup(ctx.media_channel)
                self.orch.call("call.hangup", call_id=call_id)

    # media ------------------------------------------------------------------------------------------------------
    def _on_audio(self, call_id: str, pcm: bytes) -> None:
        ctx = self.calls.get(call_id)
        if ctx is None:
            return
        frame = np.frombuffer(pcm, dtype=np.int16)
        ctx.samples += len(frame)
        for ev in ctx.detector.push(frame):
            if isinstance(ev, DigitEnd):
                for accepted in ctx.arbiter.on_inband(ev.digit, ev.t_start, ev.t_end, detector_version=ev.detector_version):
                    if not accepted.shadow:
                        self.orch.call("call.dtmf", call_id=call_id, digit=accepted.digit, source=accepted.source)
        for promoted in ctx.arbiter.tick(ctx.samples):  # in-band fallback when RFC 4733 never confirmed the digit
            self.orch.call("call.dtmf", call_id=call_id, digit=promoted.digit, source=promoted.source)
        # speech path: buffer while the orchestrator has a `listen` armed; endpointing/ASR happen in the ASR module
        ctx.speech_buf += pcm
        if len(ctx.speech_buf) >= 16000 * 2 * 5:  # 5 s horizon: flush to the ASR gateway
            chunk, ctx.speech_buf = bytes(ctx.speech_buf), bytearray()
            self.orch.call("call.audio", call_id=call_id, pcm_b64=base64.b64encode(chunk).decode(), rate=8000)

    def _on_media_closed(self, call_id: str) -> None:
        pass

    def _poll_prompts(self) -> None:
        """Pull synthesised prompts from the orchestrator and play them (push would need a callback URL)."""
        while True:
            try:
                for call_id in list(self.calls):
                    res = self.orch.call("call.next_prompt", call_id=call_id)
                    p = res.get("payload") or {}
                    if p.get("pcm_b64"):
                        self.audio.play(call_id, base64.b64decode(p["pcm_b64"]))
                        self.orch.call("call.prompt_done", call_id=call_id, prompt_id=p.get("prompt_id"))
            except Exception as exc:
                print(f"[ari] prompt loop: {exc}", flush=True)
            time.sleep(0.05)


def main() -> int:
    ari = Ari(os.environ.get("ARI_URL", "http://127.0.0.1:8088"), os.environ.get("ARI_USER", "calls"), os.environ.get("ARI_PASSWORD", ""))
    orch = OrchestratorClient(os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8080"))
    AriBridge(ari, orch, os.environ.get("AUDIOSOCKET_BIND", "0.0.0.0:9092")).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
