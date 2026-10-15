"""Run one module as an HTTP service:  python -m calls.services <crm|tts|nlu|sms|operator|asr|orchestrator|stand> [--port N]

Stand (organisers' CRM) wiring – see ``calls.crm.stand``:
  input   CRM_SCHEDULE (xlsx/csv), CRM_PATIENTS (csv/xlsx), CRM_BOOKINGS (csv/json/xlsx), or CRM_SEED_DEMO=1
  output  STAND_URL + STAND_TOKEN (+ STAND_ENDPOINTS json, STAND_RPM=10, STAND_JOURNAL jsonl, STAND_MODE=live|dry|off)
          → book / reschedule / cancel / confirm / transfer are POSTed by the ``crm`` and ``operator`` services
            (or by ``orchestrator`` when those modules run in-process).  ``POST /v1/stand.status`` shows the sync state.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from calls.crm.registry import MockRegistry, seed_demo
from calls.crm.stand import StandSync, load_inputs_from_env

from .http import make_server


def _with_stand_ops(handler, stand: StandSync | None, reg: MockRegistry):  # noqa: ANN001,ANN202
    """``stand.status`` / ``stand.flush`` next to the module's own ops (observability + the test-case runner)."""

    def wrapped(op: str, args: dict) -> tuple[str, dict]:
        if op == "stand.status":
            return "ok", stand.status() if stand else {"mode": "off", "mirror": reg.sync_report()}
        if op == "stand.flush":
            ok = stand.join(float(args.get("timeout", 30))) if stand else True
            return ("ok" if ok else "timeout"), (stand.status() if stand else {"mode": "off"})
        return handler(op, args)

    return wrapped


def frozen_now() -> datetime | None:
    raw = os.environ.get("CRM_NOW", "").strip()
    return datetime.fromisoformat(raw) if raw else None


def today() -> date:
    f = frozen_now()
    return f.date() if f else date.today()


def _log_stand(module: str, stand: StandSync | None) -> None:
    if stand is None:
        print(f"[{module}] stand: off (set STAND_URL/STAND_TOKEN or STAND_MODE=dry)", flush=True)
    else:
        print(f"[{module}] stand: mode={stand.mode} url={stand.client.base_url if stand.client else '-'} rpm={stand.client.bucket.rate if stand.client else '-'}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("module", choices=["crm", "tts", "nlu", "sms", "operator", "asr", "orchestrator", "stand"])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))  # nosec B104  # configurable; containers listen on all interfaces
    ap.add_argument("--db", default=os.environ.get("CRM_DB", ":memory:"))
    ap.add_argument("--schedule", default=os.environ.get("CRM_SCHEDULE", ""), help="CSV/XLSX schedule to import")
    ap.add_argument("--seed-demo", action="store_true", default=os.environ.get("CRM_SEED_DEMO", "1") == "1")
    ap.add_argument("--tts-model", default=os.environ.get("TTS_MODEL", "modules/tts/models/v5_cis_base.pt"))
    ap.add_argument("--tts-speaker", default=os.environ.get("TTS_SPEAKER") or None, help="ru_* for v5_cis_base (default ru_zhadyra)")
    ap.add_argument("--tts-stress", default=os.environ.get("TTS_STRESS", "auto"), choices=["auto", "dict", "silero", "off"])
    ap.add_argument("--stand-url", default=os.environ.get("STAND_URL", ""), help="demo-stand base URL (writes only, 10 RPM)")
    ns = ap.parse_args(argv)

    if ns.module == "stand":  # the fake stand – a stand-in for the organisers' CRM (e2e / test cases)
        from calls.crm.fake_stand import main as stand_main

        return stand_main(["--host", ns.host, "--port", str(ns.port)] + (["--seed-demo"] if ns.seed_demo else []))

    frozen = frozen_now()  # CRM_NOW=2026-10-14T09:00 → deterministic demo/e2e clock (unset in production)
    reg = MockRegistry(ns.db, now=frozen)
    loaded = load_inputs_from_env(reg, {**os.environ, "CRM_SCHEDULE": ns.schedule} if ns.schedule else None, log=lambda m: print(f"[{ns.module}] {m}", flush=True))
    if not loaded.get("slots") and ns.seed_demo:
        seed_demo(reg, today())

    if ns.stand_url:
        os.environ["STAND_URL"] = ns.stand_url
    stand = None
    if ns.module in ("crm", "operator"):
        stand = StandSync.from_env(reg)
        _log_stand(ns.module, stand)

    if ns.module == "crm":
        from calls.crm.module import CrmModule

        handler = _with_stand_ops(CrmModule(reg, today=today, on_write=stand.on_write if stand else None).handle, stand, reg)
    elif ns.module == "tts":
        from calls.tts.silero import TtsModule, load_synth

        synth = load_synth(ns.tts_model, ns.tts_speaker, stress=ns.tts_stress)
        print(f"[tts] synthesiser: {synth.name} speaker={getattr(synth, 'speaker', '-')} stress={getattr(getattr(synth, 'accentor', None), 'name', '-')}", flush=True)
        handler = TtsModule(synth).handle
    elif ns.module == "nlu":
        from calls.orchestrator.modules import NluModule

        nlu = NluModule()
        if os.environ.get("NLU_EMBEDDER", "hash") == "bge":
            try:
                from calls.nlu.agent import TwoAgentNLU
                from calls.nlu.embed import EmbeddingIntentClassifier, SentenceTransformerEmbedder

                nlu = NluModule(TwoAgentNLU(embed=EmbeddingIntentClassifier(SentenceTransformerEmbedder()).fit()))
                print("[nlu] verifier: deepvk/USER-bge-m3", flush=True)
            except Exception as exc:  # pragma: no cover - model/package absent
                print(f"[nlu] bge embedder unavailable ({exc}); using hash embedder", flush=True)

        def handler(op: str, args: dict) -> tuple[str, dict]:  # type: ignore[misc]
            status, payload = nlu.handle(op, args)
            payload.pop("outcome", None)  # not JSON-serialisable; remote callers use normalized/intent/decision
            return status, payload
    elif ns.module == "sms":
        from calls.notify.sms import MockSmsGateway, SmsModule

        handler = SmsModule(MockSmsGateway(), lambda v: (reg.patient_by_id(int(v["patient_id"])).phone if v.get("patient_id") else None)).handle
    elif ns.module == "operator":
        from calls.orchestrator.modules import OperatorModule

        handler = _with_stand_ops(OperatorModule(reg, on_ticket=(lambda t: stand.on_write("transfer", t)) if stand else None).handle, stand, reg)
    elif ns.module == "asr":
        from calls.orchestrator.modules import AsrModule

        asr = AsrModule()
        if os.environ.get("ASR_ENGINE", "stub") == "onnx":
            try:
                from calls.asr.onnx_gateway import OnnxAsrModule

                asr = OnnxAsrModule(os.environ.get("ASR_MODEL", "gigaam-v3-e2e-rnnt"))
                print(f"[asr] engine: onnx-asr {asr.model_name}", flush=True)
            except Exception as exc:  # pragma: no cover - package/weights absent
                print(f"[asr] onnx-asr unavailable ({exc}); using stub", flush=True)
        handler = asr.handle
    else:
        from calls.orchestrator.app import Orchestrator

        orch = Orchestrator.from_env(reg)  # owns the stand outbox only while crm/operator run in-process
        _log_stand(ns.module, orch.stand)
        handler = orch.handle

    srv = make_server(ns.module, handler, ns.host, ns.port)
    print(f"[{ns.module}] listening on {ns.host}:{ns.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
