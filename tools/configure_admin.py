from getpass import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))
from clinic.editor_auth import password_hash

if __name__ == "__main__":
    password = getpass("New admin password (12+ chars): ")
    if getpass("Repeat password: ") != password:
        raise SystemExit("Passwords do not match")
    folder = ROOT / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = Path(os.environ.get("EDITOR_PASSWORD_FILE", str(folder / "admin-password.json")))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(password_hash(password)))
    os.chmod(path, 0o600)
    print("Saved locally; restart the editor to invalidate existing sessions.")
