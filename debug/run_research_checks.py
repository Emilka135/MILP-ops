"""Research acceptance: disabled catalog, production approval gate and input provenance.

Run: python debug/run_research_checks.py [--online]. Manufactured approvals exist
only in memory. The --online option checks availability, not medical validity.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "debug/runs/research"
OFFICIAL_HOSTS = {
    "new.nmicr.ru",
    "gnicpm.ru",
    "org.gnicpm.ru",
    "udino.nmicrk.ru",
    "www.guysandstthomas.nhs.uk",
    "www.cuh.nhs.uk",
    "leaflets.ekhuft.nhs.uk",
    "www.nhs.uk",
}
INPUTS = {
    "28e5dadf-0f5f-4a36-9bca-d6e36e808b2e.docx": "0d39c2570371e499f177d91241a59009de54dfb431aea23045d21a0eb95364fc",
    "Расписание_врачей_25.06-31.12.2026.xlsx": "1f1d1025a2fe74f245e200f2656b72b0ba678ac18a4b04eb45c68dd2937fae6c",
    "30aef650-1a0b-4238-8c14-2dba969a1a2c.csv": "1616223589475b99ec228ec37bb4cc922eb9189e2a2f267624de2e63b8b29677",
    "21de07bd-6bc7-4d24-9aa1-15f9f32fd12a.csv": "bca1304ebe4c5fe3459ee1ac9b182ec2baef7bc220860ce48a16af834a4c3eb0",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def catalog_checks(data):
    if data["enabled"] is not False or data["review_required"] is not True:
        raise ValueError("Research catalog must remain disabled pending clinic review")
    sources = {s["id"] for s in data["sources"]}
    if len(sources) != len(data["sources"]):
        raise ValueError("Duplicate source IDs")
    for source in data["sources"]:
        parsed = urlparse(source["url"])
        if (
            parsed.scheme != "https"
            or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.username
        ):
            raise ValueError(f"Not a reviewed official HTTPS source: {source['id']}")
        date.fromisoformat(source["verified_on"])
        if not all(
            source.get(k)
            for k in ("organization", "evidence_role", "license", "verification")
        ):
            raise ValueError(
                f"Missing source attribution or verification: {source['id']}"
            )
    ids = set()
    for record in [data, *data["procedures"]]:
        if record["enabled"] is not False or record["review_required"] is not True:
            raise ValueError("Research instructions unexpectedly enabled")
        if any(
            record.get(k) is not None
            for k in ("clinic_id", "approved_by", "approved_at", "review_due_on")
        ):
            raise ValueError("Research record contains a clinical approval")
    for item in data["procedures"]:
        if item["id"] in ids or not 0 <= item["lead_time_hours"] < 24 * 365:
            raise ValueError(f"Invalid procedure ID/lead time: {item['id']}")
        ids.add(item["id"])
        if not all(
            item.get(k)
            for k in (
                "lead_time_basis",
                "clinical_restrictions",
                "reminder_text_ru",
                "aliases",
            )
        ):
            raise ValueError(f"Missing clinical boundaries: {item['id']}")
        if not item.get("source_ids") or not set(item["source_ids"]) <= sources:
            raise ValueError(f"Unknown source: {item['id']}")
        if any(
            i["source_id"] not in item["source_ids"]
            for i in item["source_preparation_intervals"]
        ):
            raise ValueError(f"Unrelated interval source: {item['id']}")
    for key in (
        "automatic_invitation_allowed_while_disabled",
        "automatic_medication_advice_allowed",
    ):
        if data["dispatch_contract"][key] is not False:
            raise ValueError("Unsafe dispatch contract")
    return {
        "name": "catalog_structure_and_disabled_state",
        "status": "passed",
        "procedures": len(ids),
        "sources": len(sources),
    }


def gate_checks(data):
    path = ROOT / "runtime/src/clinic/queueing.py"
    spec = importlib.util.spec_from_file_location("research_acceptance_queue", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    at = datetime.fromisoformat("2026-10-16T11:00:00+04:00")
    checks = []

    def run(name, copy, code, hours, confirmed, expected):
        queue = module.WaitingQueue.__new__(module.WaitingQueue)
        queue.preparation = copy
        actual = queue.preparation_ok(code, at + timedelta(hours=hours), at, confirmed)
        checks.append(
            {
                "name": name,
                "status": "passed" if actual == expected else "failed",
                "expected": list(expected),
                "actual": list(actual),
            }
        )

    def synthetic():
        copy = deepcopy(data)
        for record in [copy, *copy["procedures"]]:
            record.update(
                enabled=True,
                review_required=False,
                clinic_id="SYNTHETIC-TEST-ONLY",
                approved_by="synthetic-staff",
                approved_at="2026-10-15T10:00:00+04:00",
                review_due_on="2026-10-17",
            )
        return copy

    for item in data["procedures"]:
        run(
            f"disabled_{item['id']}_confirmation_cannot_bypass",
            deepcopy(data),
            item["id"],
            240,
            True,
            (False, "clinic_review_required"),
        )
    code = "colonoscopy"
    run(
        "unknown_procedure",
        synthetic(),
        "unknown-test",
        240,
        True,
        (False, "unknown_procedure"),
    )
    run(
        "next_day_colonoscopy",
        synthetic(),
        code,
        24,
        True,
        (False, "insufficient_preparation_time"),
    )
    run(
        "individual_plan_not_confirmed",
        synthetic(),
        code,
        96,
        False,
        (False, "individual_preparation_not_confirmed"),
    )
    run(
        "synthetic_approved_profile",
        synthetic(),
        code,
        96,
        True,
        (True, "approved_protocol"),
    )
    for name, target, changes, reason in [
        (
            "review_not_complete",
            "procedure",
            {"review_required": True},
            "clinic_review_required",
        ),
        ("catalog_disabled", "catalog", {"enabled": False}, "clinic_review_required"),
        (
            "missing_catalog_approval",
            "catalog",
            {"approved_by": None},
            "clinic_approval_missing",
        ),
        (
            "missing_procedure_approval",
            "procedure",
            {"approved_at": None},
            "clinic_approval_missing",
        ),
        (
            "expired_catalog",
            "catalog",
            {"review_due_on": "2026-10-15"},
            "clinic_approval_expired",
        ),
        (
            "expired_procedure",
            "procedure",
            {"review_due_on": "2026-10-15"},
            "clinic_approval_expired",
        ),
        (
            "future_approval",
            "procedure",
            {"approved_at": "2026-10-17T10:00:00+04:00"},
            "clinic_approval_not_effective",
        ),
        (
            "invalid_approval_date",
            "procedure",
            {"review_due_on": "not-a-date"},
            "invalid_clinic_approval",
        ),
        (
            "clinic_mismatch",
            "procedure",
            {"clinic_id": "OTHER-SYNTHETIC-CLINIC"},
            "clinic_approval_mismatch",
        ),
    ]:
        copy = synthetic()
        record = (
            copy
            if target == "catalog"
            else next(p for p in copy["procedures"] if p["id"] == code)
        )
        record.update(changes)
        run(name, copy, code, 240, True, (False, reason))
    return checks, digest(path)


def check_http(source):
    result = {"id": source["id"], "url": source["url"]}
    try:
        request = Request(
            source["url"], headers={"User-Agent": "ClinicResearchLinkCheck/1.0"}
        )
        with urlopen(request, timeout=12) as response:  # nosec B310: official HTTPS hosts validated above
            parsed = urlparse(response.url)
            if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
                raise ValueError("Redirect left reviewed official HTTPS hosts")
            payload = response.read(2 * 1024 * 1024 + 1)
            result.update(
                status="reachable",
                http_status=response.status,
                final_url=response.url,
                content_type=response.headers.get("Content-Type"),
                bytes_sampled=len(payload),
                sample_truncated=len(payload) > 2 * 1024 * 1024,
                sample_sha256=hashlib.sha256(payload).hexdigest(),
            )
    except (HTTPError, URLError, OSError, ValueError) as error:
        result.update(status="unverified_http", error=str(error))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    path = ROOT / "data/preparation/catalog.json"
    report = {
        "status": "failed",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "clinical_activation": False,
        "clinical_acceptance": "pending_clinic_approval",
        "clinic_identity": "unknown; synthetic input phone does not identify a clinic",
        "checks": [],
        "inputs": [],
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        report["checks"].append(catalog_checks(data))
        before = digest(path)
        cases, production_hash = gate_checks(data)
        report["checks"].extend(cases)
        if before != digest(path):
            raise ValueError("Acceptance changed the real catalog")
        report.update(catalog_sha256=before, production_gate_sha256=production_hash)
        for name, expected in INPUTS.items():
            original = ROOT.parent / name
            if original.exists():
                actual = digest(original)
                if actual != expected:
                    raise ValueError(f"Original input hash mismatch: {name}")
                report["inputs"].append(
                    {
                        "file": name,
                        "sha256": actual,
                        "bytes": original.stat().st_size,
                        "verified": True,
                    }
                )
            else:
                report["inputs"].append(
                    {
                        "file": name,
                        "sha256": expected,
                        "verified": False,
                        "reason": "original absent; not verified by this run",
                    }
                )
        report["source_review"] = [
            {k: s[k] for k in ("id", "url", "verified_on", "verification")}
            for s in data["sources"]
        ]
        if args.online:
            with ThreadPoolExecutor(max_workers=4) as pool:
                report["source_http_checks"] = list(
                    pool.map(check_http, data["sources"])
                )
            reachable = sum(
                s["status"] == "reachable" for s in report["source_http_checks"]
            )
            report["source_http_summary"] = {
                "status": "passed"
                if reachable == len(data["sources"])
                else "partially_verified",
                "reachable": reachable,
                "unverified": len(data["sources"]) - reachable,
                "clinical_fact_validation": "not_established_by_http_availability",
            }
        else:
            report["source_http_checks"] = {
                "status": "not_run",
                "reason": "Use --online; manual browser review is recorded separately.",
            }
        report["status"] = (
            "passed"
            if all(c["status"] == "passed" for c in report["checks"])
            else "failed"
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        AttributeError,
        ImportError,
    ) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    report["limitations"] = [
        "No clinic identified or protocol clinically approved.",
        "Technical gate checks do not establish individual medical suitability.",
        "URL availability and search excerpts do not prove local clinical currency.",
        "No medication dose or cancellation advice validated.",
    ]
    (OUT / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "clinical_activation": False,
                "clinical_acceptance": report["clinical_acceptance"],
                "report": str(OUT / "result.json"),
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
