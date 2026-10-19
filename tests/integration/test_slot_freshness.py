"""Live offers require a recent complete occupied-visits reconciliation."""
from datetime import date, datetime
from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm


def test_unsynced_expired_and_failed_refresh_never_offer_live_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_NOW", "2026-10-15T11:00:00+04:00")
    reg = MockRegistry(str(tmp_path / "fresh.sqlite"), datetime(2026,10,15,11))
    seed_demo(reg, date(2026,10,16))
    class OccupancyApi:
        fail = False
        def pages(self, path):
            if self.fail: raise ValueError("incomplete snapshot")
            if path == "/doctors":
                return [{"id":d["id"],"full_name":d["full_name"],"specialty":d["specialty"],"is_active":True} for d in reg.doctors()]
            assert path == "/visits"
            return []
    remote = OccupancyApi()
    crm = ClinicCrm(reg, remote)
    request = {"patient_id":3,"specialty":"терапевт"}
    assert crm.handle("agent_slots", request)[1]["error"] == "availability_not_synchronized"
    assert crm.pull()[0] == "ok"
    status, payload = crm.handle("agent_slots", request)
    assert status == "ok" and payload["slots"]
    assert payload["availability_source"] == "imported_schedule_and_synced_visits"
    crm._last_slots_sync -= 301
    assert crm.handle("agent_slots", request)[0] == "error"
    crm.pull()
    remote.fail = True
    try: crm.pull()
    except ValueError: pass
    else: raise AssertionError("failed sync cannot look successful")
    assert crm.handle("agent_slots", request)[0] == "error"
    crm.stand = None
    assert crm.handle("agent_slots", request)[1]["availability_source"] == "local_demo_schedule"
