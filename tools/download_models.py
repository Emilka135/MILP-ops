"""Fetch local artifacts before deployment; record URL and SHA256, never send patient data."""

import argparse
import hashlib
import fnmatch
import json
from pathlib import Path
import sys
import urllib.request
from check_space import require_space

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tts", action="store_true")
    p.add_argument("--asr", action="store_true")
    p.add_argument("--quantization", default="int8", choices=["int8", "none"])
    ns = p.parse_args()
    require_space(ROOT / "modules", 2 * 1024**3)
    evidence = {}
    if ns.tts:
        url = "https://models.silero.ai/models/tts/ru/v5_cis_base.pt"
        path = ROOT / "modules/tts/models/v5_cis_base.pt"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temp = path.with_suffix(".download")
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                temp.open("wb") as f,
            ):
                while data := response.read(1024 * 1024):
                    require_space(path.parent, len(data))
                    f.write(data)
            temp.replace(path)
        expected = "ba41b18f6a707ad93605a162998865e7c087153d2e010a26dd02229dab0e672a"
        if hashlib.file_digest(path.open("rb"), "sha256").hexdigest() != expected:
            raise ValueError("TTS model SHA256 mismatch")
        evidence["tts"] = {
            "license": "MIT",
            "license_url": "https://github.com/snakers4/silero-models/blob/master/LICENSE_CIS",
            "url": url,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    if ns.asr:
        from huggingface_hub import snapshot_download, model_info

        repo = "istupakov/gigaam-v3-onnx"
        info = model_info(repo, files_metadata=True)
        revision = info.sha
        target = ROOT / "modules/asr/models"
        from onnx_asr.models.gigaam import GigaamV3E2eRnnt

        quantization = None if ns.quantization == "none" else ns.quantization
        files = list(GigaamV3E2eRnnt._get_model_files(quantization).values())
        selected = [s for s in info.siblings if any(fnmatch.fnmatch(s.rfilename, pattern) for pattern in files)]
        large = [s for s in selected if s.size > 32*1024*1024]
        snapshot_download(
            repo,
            revision=revision,
            local_dir=str(target),
            allow_patterns=["config.json", "README.md", "LICENSE*", *[s.rfilename for s in selected if s not in large]],
        )
        from model_fetch import fetch
        for asset in large:
            sha = asset.lfs.sha256
            fetch(f"https://huggingface.co/{repo}/resolve/{revision}/{asset.rfilename}", target/asset.rfilename, asset.size, sha)
        evidence["asr"] = {
            "repo": repo,
            "quantization": quantization,
            "revision": revision,
            "files": {
                str(x.relative_to(target)): hashlib.sha256(x.read_bytes()).hexdigest()
                for x in target.rglob("*")
                if x.is_file() and ".cache" not in x.parts
            },
        }
    output = ROOT / "var/model-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        evidence = {**json.loads(output.read_text()), **evidence}
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
