"""Call recordings → transcripts → clusters → switch-case skeleton.

If the organisers hand out real call recordings, this tool turns them into the data the dialogue graph
needs: which phrasings occur, how often, and which of them the current switch-case already handles.

    python tools/cluster_calls.py recordings/ --asr stub                # *.wav → stub ASR (filename.txt side-car or empty)
    python tools/cluster_calls.py recordings/ --asr onnx                # *.wav → onnx-asr gigaam-v3-e2e-rnnt (pip install onnx-asr)
    python tools/cluster_calls.py recordings/ --asr deeray --url http://asr:8080   # Deeray runtime POST /v1/transcribe
    python tools/cluster_calls.py transcripts.jsonl                     # already transcribed: {"text": ...} per line
    python tools/cluster_calls.py transcripts.txt --k 12 --flow out/clusters.flow --report out/clusters.md

Pipeline (pure numpy, no sklearn):
  1. normalise (``calls.nlu.normalize.tokenize`` – fillers dropped, numbers/dates unified)
  2. hashed char-3/4-gram TF-IDF vectors (2^16 dims, L2-normalised)
  3. spherical k-means (k from ``--k`` or √(n/2)), 20 restarts, best inertia
  4. each cluster is labelled by majority vote of the rule classifier; clusters whose majority is
     ``unknown`` or whose purity < 0.6 are the ones that need new rules / new flow states
  5. emits a Markdown report and a ``.flow`` skeleton: one ``intent`` edge per labelled cluster plus
     ``# TODO`` comments with the top phrases of unlabelled clusters.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calls.nlu.intent import classify  # noqa: E402
from calls.nlu.normalize import tokenize  # noqa: E402

DIM = 1 << 16


# ---- input --------------------------------------------------------------------------------------------


def load_texts(path: Path, asr: str, url: str) -> list[tuple[str, str]]:
    """Return (source, text) pairs."""
    if path.is_dir():
        wavs = sorted(path.glob("*.wav"))
        return [(w.name, transcribe(w, asr, url)) for w in wavs]
    if path.suffix == ".jsonl":
        out = []
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if line.strip():
                obj = json.loads(line)
                out.append((obj.get("id", str(i)), obj["text"]))
        return out
    return [(str(i), line) for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()) if line.strip()]


def transcribe(wav: Path, asr: str, url: str) -> str:
    side = wav.with_suffix(".txt")
    if asr == "stub":
        return side.read_text(encoding="utf-8").strip() if side.exists() else ""
    if asr == "onnx":
        import onnx_asr  # type: ignore

        model = _onnx_model.setdefault("m", onnx_asr.load_model("gigaam-v3-e2e-rnnt"))
        return str(model.recognize(str(wav)))
    if asr == "deeray":
        import urllib.request

        data = wav.read_bytes()
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/transcribe?model=rnnt", data=data, headers={"Content-Type": "audio/wav"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())["text"]
    raise SystemExit(f"unknown asr backend {asr}")


_onnx_model: dict = {}


# ---- vectors --------------------------------------------------------------------------------------------


def ngrams(tokens: list[str]) -> list[str]:
    text = " " + " ".join(tokens) + " "
    grams = [text[i:i + n] for n in (3, 4) for i in range(len(text) - n + 1)]
    grams += ["w:" + t for t in tokens]  # whole words carry intent (отмен, перенес)
    return grams


def vectorize(texts: list[str], today: date) -> tuple[np.ndarray, list[list[str]]]:
    toks = [tokenize(t) for t in texts]
    counts = [Counter(ngrams(t)) for t in toks]
    df: Counter = Counter()
    for c in counts:
        df.update(c.keys())
    n = len(texts)
    x = np.zeros((n, DIM), dtype=np.float32)
    for i, c in enumerate(counts):
        for g, tf in c.items():
            idx = zlib.crc32(g.encode()) % DIM
            x[i, idx] += (1 + math.log(tf)) * math.log((1 + n) / (1 + df[g]))
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return x / norms, toks


def spherical_kmeans(x: np.ndarray, k: int, restarts: int = 20, iters: int = 50, seed: int = 0) -> tuple[np.ndarray, float]:
    rnd = np.random.default_rng(seed)
    best_labels, best_score = None, -1.0
    for _ in range(restarts):
        # k-means++ seeding on cosine distance
        centers = [x[rnd.integers(len(x))]]
        for _ in range(1, k):
            sims = np.max(np.stack([x @ c for c in centers]), axis=0)
            d = np.clip(1 - sims, 1e-6, None) ** 2
            centers.append(x[rnd.choice(len(x), p=d / d.sum())])
        c = np.stack(centers)
        labels = np.zeros(len(x), dtype=int)
        for _ in range(iters):
            new = np.argmax(x @ c.T, axis=1)
            if np.array_equal(new, labels):
                break
            labels = new
            for j in range(k):
                m = x[labels == j]
                if len(m):
                    v = m.sum(axis=0)
                    c[j] = v / (np.linalg.norm(v) or 1)
        score = float(np.mean(np.sum(x * c[labels], axis=1)))  # mean cosine to own centroid
        if score > best_score:
            best_score, best_labels = score, labels.copy()
    assert best_labels is not None
    return best_labels, best_score


# ---- output ----------------------------------------------------------------------------------------------


def describe(labels: np.ndarray, texts: list[str], toks: list[list[str]], today: date) -> list[dict]:
    groups: dict[int, list[int]] = defaultdict(list)
    for i, l in enumerate(labels):
        groups[int(l)].append(i)
    out = []
    for cid, idx in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        intents = Counter(classify(texts[i], today).intent for i in idx)
        top_intent, top_n = intents.most_common(1)[0]
        words = Counter(w for i in idx for w in set(toks[i]))
        out.append({
            "cluster": cid,
            "size": len(idx),
            "intent": top_intent,
            "purity": round(top_n / len(idx), 2),
            "keywords": [w for w, _ in words.most_common(6)],
            "examples": [texts[i] for i in idx[:5]],
            "needs_rules": top_intent == "unknown" or top_n / len(idx) < 0.6,
        })
    return out


def write_report(clusters: list[dict], path: Path, n: int, score: float) -> None:
    lines = [f"# Кластеры обращений ({n} реплик, {len(clusters)} кластеров, средняя косинусная близость {score:.2f})", ""]
    lines.append("| # | размер | интент (switch-case) | чистота | ключевые слова | нужны правила |")
    lines.append("|---:|---:|---|---:|---|---|")
    for c in clusters:
        lines.append(f"| {c['cluster']} | {c['size']} | `{c['intent']}` | {c['purity']} | {', '.join(c['keywords'])} | {'**да**' if c['needs_rules'] else ''} |")
    lines.append("")
    for c in clusters:
        lines.append(f"## Кластер {c['cluster']} — `{c['intent']}` ({c['size']})")
        lines.extend(f"* {e}" for e in c["examples"])
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_flow(clusters: list[dict], path: Path) -> None:
    covered = sorted({c["intent"] for c in clusters if not c["needs_rules"] and c["intent"] != "unknown"})
    lines = ["# generated by tools/cluster_calls.py – paste the relevant edges into scenarios/inbound.flow", "flow clusters", "  entry listen", "",
             "state listen", '  say "Скажите, чем могу помочь."', "  listen 8s"]
    human = {"complaint", "emergency", "medical", "operator"}
    for intent in covered:
        if intent not in human:
            lines.append(f"  intent {intent} -> handle_{intent}")
    lines.append("  intent complaint|emergency|medical|operator -> operator")
    lines.append("  silence -> operator")
    lines.append("  any -> operator")
    lines.append("  timeout 10s -> operator")
    lines.append("")
    for intent in covered:
        if intent not in human:
            lines += [f"state handle_{intent}", f'  say "TODO: {intent}"', "  end"]
            lines.append("")
    lines += ["state operator", "  transfer reason=unclustered", "  end transferred", ""]
    for c in clusters:
        if c["needs_rules"]:
            lines.append(f"# TODO cluster {c['cluster']} ({c['size']} реплик, intent={c['intent']}, purity={c['purity']}): " + " | ".join(c["examples"][:3]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="directory with *.wav (+ optional *.txt side-cars) or a .txt/.jsonl transcript file")
    ap.add_argument("--asr", choices=["stub", "onnx", "deeray"], default="stub")
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--k", type=int, default=0, help="number of clusters (default √(n/2))")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    ap.add_argument("--report", type=Path, default=Path("reports/clusters.md"))
    ap.add_argument("--flow", type=Path, default=Path("reports/clusters.flow"))
    ap.add_argument("--json", type=Path, default=None)
    ns = ap.parse_args(argv)

    pairs = [(s, t) for s, t in load_texts(ns.source, ns.asr, ns.url) if t.strip()]
    if len(pairs) < 2:
        raise SystemExit("need at least two non-empty transcripts")
    texts = [t for _, t in pairs]
    x, toks = vectorize(texts, ns.today)
    k = ns.k or max(2, min(len(texts) // 2, int(round(math.sqrt(len(texts) / 2)))))
    labels, score = spherical_kmeans(x, k)
    clusters = describe(labels, texts, toks, ns.today)
    write_report(clusters, ns.report, len(texts), score)
    write_flow(clusters, ns.flow)
    if ns.json:
        ns.json.write_text(json.dumps({"score": score, "clusters": clusters}, ensure_ascii=False, indent=2), encoding="utf-8")
    covered = sum(c["size"] for c in clusters if not c["needs_rules"])
    print(f"{len(texts)} utterances → {k} clusters (cohesion {score:.2f}); switch-case already covers {covered}/{len(texts)} "
          f"({covered / len(texts):.0%}); {sum(c['needs_rules'] for c in clusters)} clusters need new rules")
    print(f"report: {ns.report}\nflow skeleton: {ns.flow}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
