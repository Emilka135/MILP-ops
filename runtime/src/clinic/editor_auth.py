"""Password-only administration, isolated from the call API's bearer credential."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from http.cookies import SimpleCookie


def password_hash(password):
    if password != "admin" and len(password) < 12:
        raise ValueError("Пароль должен содержать не менее 12 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return {
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
    }


class EditorAuth:
    def __init__(self, path, token=""):
        self.path = Path(path)
        self.token = token
        self.lock = threading.Lock()
        self.sessions = {}
        self.attempts = {}
        if not self.path.exists():
            password = os.environ.get("EDITOR_BOOTSTRAP_PASSWORD", "admin")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.write_text(json.dumps(password_hash(password)))
            os.chmod(self.path, 0o600)
            initial = self.path.parent / "admin-initial-password.txt"
            initial.write_text(password + "\n")
            os.chmod(initial, 0o600)
            print(
                "Admin password created locally at "
                + str(initial)
                + "; it is not logged.",
                flush=True,
            )

    def allowed(self, headers):
        if self.token and hmac.compare_digest(
            headers.get("Authorization", ""), "Bearer " + self.token
        ):
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
        except Exception:
            return False
        item = cookie.get("clinic_admin")
        if not item:
            return False
        digest = hashlib.sha256(item.value.encode()).hexdigest()
        with self.lock:
            expiry = self.sessions.get(digest, 0)
            if expiry > time.time():
                return True
            self.sessions.pop(digest, None)
        return False

    def login(self, password, client):
        if not isinstance(password, str) or len(password) > 1024:
            raise ValueError("Неверный пароль")
        with self.lock:
            attempts = [
                t for t in self.attempts.get(client, []) if t > time.time() - 900
            ]
            if len(attempts) >= 5:
                raise ValueError("Слишком много попыток. Повторите через 15 минут.")
            attempts.append(time.time())
            self.attempts[client] = attempts
        record = json.loads(self.path.read_text())
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(record["salt"]), n=16384, r=8, p=1
        )
        if not hmac.compare_digest(digest, base64.b64decode(record["hash"])):
            raise ValueError("Неверный пароль")
        session = secrets.token_urlsafe(32)
        with self.lock:
            self.attempts.pop(client, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v > time.time()}
            self.sessions[hashlib.sha256(session.encode()).hexdigest()] = (
                time.time() + 8 * 3600
            )
        return session

    def logout(self, headers):
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
        except Exception:
            return
        if cookie.get("clinic_admin"):
            with self.lock:
                self.sessions.pop(
                    hashlib.sha256(cookie["clinic_admin"].value.encode()).hexdigest(),
                    None,
                )
