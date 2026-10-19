"""Local feedback, complaints, nightly thematic clustering and opt-in registrar variants."""

from collections import Counter
from contextlib import contextmanager
import threading
import json
import math
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY,call_id TEXT NOT NULL,registrar TEXT NOT NULL,text TEXT NOT NULL,kind TEXT NOT NULL,rating INTEGER,created_at TEXT NOT NULL,cluster_id INTEGER,UNIQUE(call_id,text));
CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY,feedback_id INTEGER NOT NULL UNIQUE,registrar TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open',resolution TEXT);
CREATE TABLE IF NOT EXISTS feedback_clusters(id INTEGER PRIMARY KEY,title TEXT NOT NULL,terms TEXT NOT NULL,size INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registrar_profiles(id INTEGER PRIMARY KEY,registrar TEXT NOT NULL,label TEXT NOT NULL,base_flow TEXT NOT NULL,flow_name TEXT,state TEXT NOT NULL DEFAULT 'proposed',proposal TEXT NOT NULL,created_at TEXT NOT NULL,reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS night_jobs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',created_at TEXT NOT NULL,finished_at TEXT,result TEXT);
"""
STOP = {
    "что",
    "это",
    "как",
    "для",
    "мне",
    "меня",
    "вас",
    "вам",
    "при",
    "нужно",
    "надо",
    "быть",
    "было",
    "очень",
    "чтобы",
    "просто",
    "спасибо",
    "пожалуйста",
    "хотел",
    "хочу",
    "еще",
    "можно",
    "когда",
    "говорит",
    "сказал",
    "этот",
    "этого",
    "только",
    "свой",
}
THEMES = {
    "повтор": ["повтор", "переспраш", "слыш", "тихо"],
    "темп": ["быстр", "медлен", "скорост", "темп"],
    "навигация": ["кноп", "меню", "оператор", "человек", "перевод"],
    "слоты": ["дат", "время", "запис", "слот", "талон"],
    "распознавание": ["фамил", "имя", "неверно", "распозна", "цифр"],
}


def tokens(text):
    return [w for w in re.findall(r"[а-яёa-z]{3,}", text.lower()) if w not in STOP]


def clusters(rows):
    """Sparse TF-IDF cosine, deterministic local agglomeration; no cloud/PHi export."""
    docs = [Counter(tokens(r["text"])) for r in rows]
    df = Counter(w for d in docs for w in d)
    vectors = []
    for d in docs:
        vector = {w: n * math.log(1 + len(docs) / (1 + df[w])) for w, n in d.items()}
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1
        vectors.append({w: v / norm for w, v in vector.items()})
    groups = []
    for i, vec in enumerate(vectors):
        best = None
        score = 0.27
        for j, group in enumerate(groups):
            similarity = max(
                sum(v * vectors[k].get(w, 0) for w, v in vec.items()) for k in group
            )
            if similarity > score:
                score = similarity
                best = j
        if best is None:
            groups.append([i])
        else:
            groups[best].append(i)
    result = []
    for group in groups:
        terms = Counter(w for i in group for w in docs[i]).most_common(6)
        theme_scores = {
            theme: sum(
                sum(word.startswith(prefix) for prefix in prefixes) for word, _ in terms
            )
            for theme, prefixes in THEMES.items()
        }
        theme = (
            max(theme_scores, key=theme_scores.get)
            if any(theme_scores.values())
            else "предложения"
        )
        result.append(
            {
                "title": theme,
                "terms": [w for w, _ in terms],
                "feedback_ids": [rows[i]["id"] for i in group],
            }
        )
    return result


def registrar_number(value):
    """Canonical phone/workplace identifier, never a silently truncated scope."""
    text = str(value).strip()
    if not re.fullmatch(r"\+?[0-9 ()-]{1,40}", text):
        raise ValueError("registrar_number_required")
    result = re.sub(r"[^0-9]", "", text)
    if not 1 <= len(result) <= 24:
        raise ValueError("registrar_number_required")
    return ("+" if text.startswith("+") else "") + result


class Insights:
    """Durable local jobs. ``actor`` is trusted server context, never request JSON.

    Internal RPC callers may submit call feedback and run night jobs. Human-facing
    dashboards and decisions require the authenticated actor supplied by an adapter.
    Approval is a review decision only: no proposal can route until a deployment
    verifier is implemented. No API argument can bypass that deployment gate.
    """

    def __init__(self, reg):
        self.reg = reg
        self.lock = threading.RLock()
        with reg._lock:
            reg._conn.executescript(SCHEMA)
            columns = {r[1] for r in reg._conn.execute("PRAGMA table_info(feedback)")}
            if "rating_source" not in columns:
                reg._conn.execute("ALTER TABLE feedback ADD COLUMN rating_source TEXT")
            cluster_columns = {
                r[1] for r in reg._conn.execute("PRAGMA table_info(feedback_clusters)")
            }
            if "registrar" not in cluster_columns:
                reg._conn.execute("ALTER TABLE feedback_clusters ADD COLUMN registrar TEXT")

    @contextmanager
    def _transaction(self):
        # The registry connection is shared by modules. Hold its lock for the
        # entire change so feedback+complaint+suspension commit together.
        with self.lock, self.reg._lock:
            self.reg._conn.execute("SAVEPOINT insights_operation")
            try:
                yield
            except Exception:
                self.reg._conn.execute("ROLLBACK TO insights_operation")
                self.reg._conn.execute("RELEASE insights_operation")
                raise
            else:
                self.reg._conn.execute("RELEASE insights_operation")

    def handle(self, op, args, at, closed=False, *, actor=None):
        role = actor.get("role") if actor else "system"
        if role not in {"admin", "registrar", "system"}:
            return "forbidden", {"error": "authentication_required"}
        if op in {"profile.review", "complaint.resolve"} and role != "admin":
            return "forbidden", {"error": "admin_required"}
        if op == "insights.dashboard" and role not in {"admin", "registrar"}:
            return "forbidden", {"error": "authentication_required"}
        if op in {"nightly.run", "profile.route"} and role not in {"admin", "system"}:
            return "forbidden", {"error": "internal_operation"}
        scoped = dict(args)
        if role == "registrar":
            # Ignore a registrar supplied in browser JSON, including dashboard filters.
            try:
                scoped["registrar"] = registrar_number(actor.get("registrar", ""))
            except ValueError:
                return "forbidden", {"error": "registrar_number_required"}
        try:
            with self._transaction():
                return self._handle(op, scoped, at, closed, role)
        except (ValueError, KeyError, TypeError):
            return "error", {"error": "invalid_insight_arguments"}

    def _has_table(self, name):
        return bool(self.reg.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ))

    def _suspend(self, registrar):
        self.reg._x(
            "UPDATE registrar_profiles SET state='suspended' WHERE registrar=? AND state IN('approved','active')",
            (registrar,),
        )
        if self._has_table("learning_cases"):
            self.reg._x(
                "UPDATE learning_cases SET status='suspended' WHERE registrar=? AND status IN('approved','active')",
                (registrar,),
            )

    def _queue_cluster(self, at):
        if not self.reg.execute(
            "SELECT id FROM night_jobs WHERE kind='feedback_cluster' AND status='queued'"
        ):
            self.reg._x(
                "INSERT INTO night_jobs(kind,payload,created_at) VALUES(?,?,?)",
                ("feedback_cluster", "{}", at.isoformat()),
            )

    def _handle(self, op, args, at, closed, role):
        if op == "feedback.submit":
            text = str(args.get("text", "")).strip()
            if not text:
                return "empty", {}
            call_id = str(args.get("call_id", "")).strip()
            if len(text) > 4000 or not call_id or len(call_id) > 128:
                return "error", {"error": "feedback_size_or_call_id_invalid"}
            registrar = registrar_number(args.get("registrar") or "100")
            # One call belongs to one registration phone. Validate against archived
            # calls when present and against earlier feedback even before archiving.
            existing = self.reg.execute(
                "SELECT registrar FROM feedback WHERE call_id=? LIMIT 1", (call_id,)
            )
            if self._has_table("learning_calls"):
                existing += self.reg.execute(
                    "SELECT registrar FROM learning_calls WHERE call_id=?", (call_id,)
                )
            if any(registrar_number(row["registrar"]) != registrar for row in existing):
                return "forbidden", {"error": "call_registrar_mismatch"}
            requested_kind = args.get("kind")
            if requested_kind not in {None, "complaint", "suggestion", "praise"}:
                return "error", {"error": "invalid_feedback_kind"}
            negative = bool(re.search(
                r"жалоб|ужас|\bплохо\b|недовол|неправиль|не работ|ошиб(?:ка|ки|ку|ся)",
                text.lower(),
            ))
            kind = "complaint" if requested_kind == "complaint" or negative else requested_kind or "suggestion"
            # A free answer is not a five-star vote. Only an explicit volunteered
            # number is stored as a rating; sentiment classification is separate.
            rating = args.get("rating")
            if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
                return "error", {"error": "rating_must_be_integer_1_to_5"}
            previous = self.reg.execute(
                "SELECT * FROM feedback WHERE call_id=? AND text=?", (call_id, text)
            )
            if previous:
                row = previous[0]
                if requested_kind == "complaint" and row["kind"] != "complaint":
                    self.reg._x("UPDATE feedback SET kind='complaint' WHERE id=?", (row["id"],))
                    kind = "complaint"
                else:
                    kind = row["kind"]
            else:
                fid = self.reg._x(
                    "INSERT INTO feedback(call_id,registrar,text,kind,rating,rating_source,created_at) VALUES(?,?,?,?,?,?,?)",
                    (call_id, registrar, text, kind, rating,
                     "patient_explicit" if rating is not None else None, at.isoformat()),
                )
                row = {"id": fid}
            if kind == "complaint":
                self.reg._x(
                    "INSERT OR IGNORE INTO complaints(feedback_id,registrar) VALUES(?,?)",
                    (row["id"], registrar),
                )
                if self.reg.execute(
                    "SELECT id FROM complaints WHERE feedback_id=? AND state='open'", (row["id"],)
                ):
                    self._suspend(registrar)
            self._queue_cluster(at)
            return "ok", {"feedback_id": row["id"], "kind": kind, "night_processing": "queued"}
        if op == "insights.dashboard":
            where = " WHERE registrar=?" if role == "registrar" or args.get("registrar") else ""
            params = (registrar_number(args["registrar"]),) if where else ()
            feedback = self.reg.execute("SELECT * FROM feedback" + where + " ORDER BY id DESC LIMIT 300", params)
            complaint_where = where.replace("registrar", "c.registrar")
            metrics = self.reg.execute(
                "SELECT registrar,COUNT(*) AS feedback_count,SUM(CASE WHEN kind='complaint' THEN 1 ELSE 0 END) AS complaint_count,COUNT(CASE WHEN rating_source='patient_explicit' THEN rating END) AS explicit_rating_count,AVG(CASE WHEN rating_source='patient_explicit' THEN rating END) AS explicit_rating_mean FROM feedback"
                + where + " GROUP BY registrar", params,
            )
            for row in metrics:
                row["complaint_share"] = row["complaint_count"] / row["feedback_count"]
            return "ok", {
                "feedback": feedback,
                "complaints": self.reg.execute(
                    "SELECT c.*,f.text FROM complaints c JOIN feedback f ON f.id=c.feedback_id"
                    + complaint_where + " ORDER BY c.id DESC", params,
                ),
                "clusters": self.reg.execute("SELECT * FROM feedback_clusters" + where + " ORDER BY id DESC LIMIT 300", params),
                "profiles": self.reg.execute("SELECT * FROM registrar_profiles" + where + " ORDER BY id DESC LIMIT 300", params),
                "jobs": self.reg.execute("SELECT * FROM night_jobs ORDER BY id DESC LIMIT 30") if role == "admin" else [],
                "metrics": metrics,
                "rating_note": "Средняя оценка учитывает только явно названные пациентами числа. Доля жалоб считается отдельно. Текст не превращается в звёзды; влияние правок на качество ещё не измерено.",
            }
        if op == "complaint.resolve":
            note = str(args.get("resolution", "")).strip()
            if not note or len(note) > 4000:
                return "error", {"error": "resolution_required"}
            cid = int(args["id"])
            if not self.reg.execute("SELECT id FROM complaints WHERE id=?", (cid,)):
                return "empty", {}
            self.reg._x("UPDATE complaints SET state='resolved',resolution=? WHERE id=?", (note, cid))
            # Resolving a complaint never silently restores a profile.
            return "ok", {"profile_reactivation": False}
        if op == "profile.queue":
            registrar = registrar_number(args["registrar"])
            base_flow = str(args.get("base_flow", "inbound"))
            label = str(args.get("label", "Дополнительный сценарий")).strip()
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", base_flow) or not 1 <= len(label) <= 200:
                return "error", {"error": "invalid_profile_proposal"}
            payload = {"registrar": registrar, "base_flow": base_flow, "label": label}
            # Retrying a button press must not generate duplicate nightly proposals.
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            pending = self.reg.execute(
                "SELECT id FROM night_jobs WHERE kind='profile_proposal' AND payload=? AND status='queued'", (encoded,)
            )
            jid = pending[0]["id"] if pending else self.reg._x(
                "INSERT INTO night_jobs(kind,payload,created_at) VALUES(?,?,?)",
                ("profile_proposal", encoded, at.isoformat()),
            )
            return "ok", {"job_id": jid, "status": "queued"}
        if op == "profile.review":
            rows = self.reg.execute("SELECT * FROM registrar_profiles WHERE id=?", (int(args["id"]),))
            if not rows:
                return "empty", {}
            profile = rows[0]
            if type(args.get("approve")) is not bool:
                return "error", {"error": "explicit_review_decision_required"}
            if args["approve"]:
                if self.reg.execute("SELECT id FROM complaints WHERE registrar=? AND state='open'", (profile["registrar"],)):
                    return "blocked", {"error": "unresolved_complaints"}
                if profile["state"] not in {"proposed", "suspended"}:
                    return "error", {"error": "invalid_profile_state"}
                self.reg._x(
                    "UPDATE registrar_profiles SET state='approved',reviewed_at=? WHERE id=?",
                    (at.isoformat(), profile["id"]),
                )
                return "ok", {"state": "approved", "deployment": "blocked", "reason": "deployment_verifier_required"}
            self.reg._x(
                "UPDATE registrar_profiles SET state='rejected',reviewed_at=? WHERE id=?",
                (at.isoformat(), profile["id"]),
            )
            return "ok", {"state": "rejected"}
        if op == "profile.route":
            registrar = registrar_number(args.get("registrar", ""))
            if self.reg.execute("SELECT id FROM complaints WHERE registrar=? AND state='open'", (registrar,)):
                self._suspend(registrar)
                return "empty", {"reason": "complaints_block_profile"}
            # Even legacy rows marked 'active' are not executable proof. No caller-
            # supplied flow name, timestamp, or verified=True can enable a route.
            return "empty", {"reason": "deployment_verifier_required"}
        if op == "nightly.run":
            if not closed:
                return "blocked", {"reason": "clinic_is_open", "jobs_run": 0}
            return self._night(at)
        return "error", {"error": "unknown_insight_operation"}

    def _night(self, at):
        jobs = self.reg.execute("SELECT * FROM night_jobs WHERE status='queued' ORDER BY id LIMIT 20")
        done = failed = 0
        for job in jobs:
            self.reg._conn.execute("SAVEPOINT insights_job")
            try:
                self.reg._x("UPDATE night_jobs SET status='running' WHERE id=?", (job["id"],))
                if job["kind"] == "feedback_cluster":
                    rows = self.reg.execute("SELECT id,text,registrar FROM feedback WHERE cluster_id IS NULL ORDER BY id LIMIT 2000")
                    groups = []
                    for registrar in sorted({row["registrar"] for row in rows}):
                        scoped = [row for row in rows if row["registrar"] == registrar]
                        groups.extend((registrar, group) for group in clusters(scoped))
                    for registrar, group in groups:
                        cid = self.reg._x(
                            "INSERT INTO feedback_clusters(title,terms,size,updated_at,registrar) VALUES(?,?,?,?,?)",
                            (group["title"], json.dumps(group["terms"], ensure_ascii=False),
                             len(group["feedback_ids"]), at.isoformat(), registrar),
                        )
                        for fid in group["feedback_ids"]:
                            self.reg._x("UPDATE feedback SET cluster_id=? WHERE id=?", (cid, fid))
                    result = {"clusters": len(groups), "reviews": len(rows), "method": "local_tfidf_cosine", "scope": "registrar", "batch_limit": 2000}
                elif job["kind"] == "profile_proposal":
                    payload = json.loads(job["payload"])
                    registrar = registrar_number(payload["registrar"])
                    evidence = self.reg.execute("SELECT id,text,kind FROM feedback WHERE registrar=? ORDER BY id DESC LIMIT 100", (registrar,))
                    themes = Counter(
                        theme for row in evidence for theme, prefixes in THEMES.items()
                        if any(any(w.startswith(p) for p in prefixes) for w in tokens(row["text"]))
                    )
                    proposal = {"themes": dict(themes), "evidence_ids": [r["id"] for r in evidence],
                                "evidence_count": len(evidence), "automatic_activation": False,
                                "method": "local_theme_summary", "neural_training": False}
                    pid = self.reg._x(
                        "INSERT INTO registrar_profiles(registrar,label,base_flow,proposal,created_at) VALUES(?,?,?,?,?)",
                        (registrar, payload["label"], payload["base_flow"], json.dumps(proposal, ensure_ascii=False), at.isoformat()),
                    )
                    result = {"profile_id": pid, "state": "proposed"}
                else:
                    raise ValueError("unknown_night_job_kind")
                self.reg._x(
                    "UPDATE night_jobs SET status='done',finished_at=?,result=? WHERE id=?",
                    (at.isoformat(), json.dumps(result, ensure_ascii=False), job["id"]),
                )
                self.reg._conn.execute("RELEASE insights_job")
                done += 1
            except (ValueError, KeyError, TypeError):
                self.reg._conn.execute("ROLLBACK TO insights_job")
                self.reg._conn.execute("RELEASE insights_job")
                self.reg._x(
                    "UPDATE night_jobs SET status='failed',finished_at=?,result=? WHERE id=?",
                    (at.isoformat(), json.dumps({"error": "invalid_job_payload"}), job["id"]),
                )
                failed += 1
        if self.reg.execute("SELECT id FROM feedback WHERE cluster_id IS NULL LIMIT 1"):
            self._queue_cluster(at)
        return "ok", {"jobs_run": done, "jobs_failed": failed, "status": "finished", "compute": "CPU; no GPU process control"}
