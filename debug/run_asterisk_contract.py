"""Local-only actual Asterisk ARI/AudioSocket contract; never originates a telephone call."""

import json
import os
import socket
import struct
import sys
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))
from calls.telephony.ari_bridge import Ari, WsClient
from clinic.telephony import recv_exact


def main():
    password = os.environ["ARI_PASSWORD"]
    ari = Ari("http://127.0.0.1:8088", "calls", password)
    ws = WsClient(
        "ws://127.0.0.1:8088/ari/events?app=clinic-debug&api_key="
        + urllib.parse.quote("calls:" + password, safe="")
    )
    channel = None
    conn = None
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)
    call = str(uuid.uuid4())
    try:
        channel = ari.external_media(
            "clinic-debug", "127.0.0.1:" + str(server.getsockname()[1]), call
        )
        conn, _ = server.accept()
        conn.settimeout(10)
        kind, length = struct.unpack("!BH", recv_exact(conn, 3))
        received = recv_exact(conn, length)
        assert kind == 1 and length == 16 and str(uuid.UUID(bytes=received)) == call, (
            kind,
            length,
        )
        report = {
            "asterisk": "22.11.0",
            "results": {
                "ari_websocket": "passed",
                "external_media_tcp": "passed",
                "binary_uuid": "passed",
            },
            "external_channel": channel["name"],
            "phone_calls_originated": 0,
        }
        folder = ROOT / "debug/runs/containers"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "asterisk-contract.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps(report, indent=2))
    finally:
        if channel:
            ari.hangup(channel["id"])
        if conn:
            conn.close()
        server.close()
        ws.sock.close()


if __name__ == "__main__":
    main()
