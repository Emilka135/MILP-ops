from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path

from calls.crm.registry import MockRegistry, seed_demo
from calls.services.http import make_server

from .crm import ClinicCrm, StrictStand, now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "module",
        choices=[
            "crm",
            "nlu",
            "tts",
            "asr",
            "sms",
            "operator",
            "stand",
            "outbound",
            "agent",
            "nightly",
        ],
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8081")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ns = parser.parse_args()
    if ns.module == "stand":
        from .stand import make_stand_server

        server = make_stand_server(ns.host, ns.port)
    elif ns.module == "nightly":
        from .nightly import run

        return run()
    elif ns.module == "outbound":
        from .outbound import run

        return run()
    else:
        if ns.module == "crm":
            path = os.environ.get("CRM_DB", "var/clinic.sqlite")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            reg = MockRegistry(path, now=now().replace(tzinfo=None))
            if not os.environ.get("CRM_NOW"):
                reg._now = None
                reg.now = lambda: now().replace(tzinfo=None)
            service = ClinicCrm(reg)
            from .inputs import load

            loaded = load(reg, os.environ)
            demo = os.environ.get("APP_MODE", "demo") == "demo"
            if not loaded.get("slots") and demo:
                seed_demo(reg, now().date() + __import__("datetime").timedelta(days=1))
            from .stand_config import settings

            connection = settings(os.environ)
            stand = StrictStand(*connection) if connection else None
            service.stand = stand
            if stand:
                threading.Thread(target=service.refresh_availability, daemon=True).start()
        else:
            from .modules import Nlu, Operator, Sms, Speech

            if ns.module == "agent":
                from .agent import SlotAgent

                service = SlotAgent()
            elif ns.module == "nlu":
                service = Nlu()
            elif ns.module in ("tts", "asr"):
                service = Speech(ns.module)
            elif ns.module == "operator":
                service = Operator()
            else:
                service = Sms()
                threading.Thread(target=service.run, daemon=True).start()
        server = make_server(ns.module, service.handle, ns.host, ns.port)
    print(f"{ns.module} listening on {ns.host}:{ns.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
