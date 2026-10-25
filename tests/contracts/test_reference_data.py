"""Keep bundled case exports byte-identical on clone, including CSV CRLF."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def test_bundled_exports_match_the_reviewed_manifest():
    folder=ROOT/'reference-data'
    manifest=json.loads((folder/'manifest.json').read_text())
    assert len(manifest['files'])==3
    for item in manifest['files']:
        assert Path(item['file']).name==item['file']
        raw=(folder/item['file']).read_bytes()
        assert len(raw)==item['bytes']
        assert hashlib.sha256(raw).hexdigest()==item['sha256']
