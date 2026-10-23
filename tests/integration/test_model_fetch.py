import hashlib
import io
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import model_fetch


def test_verified_download_removes_parts_and_cached_model_is_not_reloaded(tmp_path, monkeypatch):
    raw = b'not-a-real-model:download-integrity-fixture'
    class Response(io.BytesIO):
        headers = {'Content-Range': f'bytes 0-{len(raw)-1}/{len(raw)}'}
    calls = []
    def download(request, timeout):
        calls.append(request.full_url)
        return Response(raw)
    monkeypatch.setattr(model_fetch.urllib.request, 'urlopen', download)
    monkeypatch.setattr(model_fetch, 'require_space', lambda *args: None)
    path = tmp_path / 'model.bin'
    model_fetch.fetch('https://example.invalid/model', path, len(raw), hashlib.sha256(raw).hexdigest())
    assert path.read_bytes() == raw
    assert not (tmp_path / '.cache/ranges/model.bin').exists()
    model_fetch.fetch('https://example.invalid/model', path, len(raw), hashlib.sha256(raw).hexdigest())
    assert len(calls) == 1
