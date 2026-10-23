import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from calls.crm.module import CrmModule
from calls.crm.registry import MockRegistry, seed_demo
from calls.scenario.dsl import load_flows

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 14)  # Wednesday
NOW = datetime(2026, 10, 14, 9, 0)


@pytest.fixture(scope="session")
def flows():
    return load_flows(ROOT / "scenarios")


@pytest.fixture
def reg():
    r = MockRegistry(now=NOW)
    seed_demo(r, TODAY)
    return r


@pytest.fixture
def crm(reg):
    return CrmModule(reg, today=lambda: TODAY)


@pytest.fixture
def known_caller():
    return {"caller_phone": "79035551234", "today": TODAY.isoformat(), "clinic_name": "поликлиники номер семь", "clinic_phone": "+7 000 000-00-00"}


def wait_until(pred, timeout: float = 5.0, step: float = 0.01):
    """Poll ``pred`` until true (bounded) – for work that finishes on bus worker threads; never a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def pytest_runtest_logreport(report):
    """On GitHub Actions, surface every failure as a workflow annotation (visible in the run summary and via
    the API even when the raw logs are not reachable)."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not report.failed:
        return
    path, line, _ = report.location
    lines = report.longreprtext.strip().splitlines()
    tail = " | ".join(lines[-15:]).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    title = f"{report.nodeid} ({report.when})".replace(",", ";")
    sys.stdout.write(f"\n::error file={path},line={(line or 0) + 1},title={title}::{tail[:3800]}\n")
    sys.stdout.flush()
