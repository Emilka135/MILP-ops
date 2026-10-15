"""Password-only administration, isolated from the call API's bearer credential."""

import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import threading
import time
from http.cookies import SimpleCookie


def password_hash(password):
    if password not in {"admin", "user"} and len(password) < 12:
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

        if 'user' not in self.accounts():
            self.add_account('user','100','user','Регистратор')

    def identity(self, headers):
        if self.token and hmac.compare_digest(headers.get('Authorization',''), 'Bearer '+self.token):
            return {'username':'admin','role':'admin','registrar':None}
        cookie=SimpleCookie()
        try:cookie.load(headers.get('Cookie',''))
        except Exception:return None
        item=cookie.get('clinic_admin')
        if not item:return None
        digest=hashlib.sha256(item.value.encode()).hexdigest()
        with self.lock:
            session=self.sessions.get(digest)
            if session and session['expires']>time.time():return dict(session['identity'])
            self.sessions.pop(digest,None)
        return None

    def allowed(self, headers):
        return self.identity(headers) is not None

    def accounts(self):
        path=self.path.parent/'registrar-accounts.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def add_account(self, username, registrar, password, label=''):
        if not isinstance(username,str) or not re.fullmatch('[a-zA-Z0-9_]{3,40}',username) or username=='admin':
            raise ValueError('Логин: 3–40 латинских букв, цифр или _')
        if not re.fullmatch('[+0-9]{1,24}',str(registrar)):
            raise ValueError('Укажите номер рабочего места')
        if not isinstance(password,str) or (len(password)<12 and not (username=='user' and password=='user')):
            raise ValueError('Пароль регистратора — не менее 12 символов')
        with self.lock:
            accounts=self.accounts()
            accounts[username]={'registrar':str(registrar),'label':str(label)[:100],'password':password_hash(password)}
            path=self.path.parent/'registrar-accounts.json'
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(accounts,ensure_ascii=False));os.chmod(temp,0o600);os.replace(temp,path)
            self.sessions={k:v for k,v in self.sessions.items() if v['identity']['username']!=username}
        return {'username':username,'registrar':str(registrar),'label':str(label)[:100]}

    def login(self, password, client, username="admin"):
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
        account=self.accounts().get(str(username)) if username!='admin' else None
        identity={'username':str(username),'role':'admin' if username=='admin' else 'registrar','registrar':account['registrar'] if account else None,'label':account.get('label','') if account else 'Администратор'}
        record = account['password'] if account else json.loads(self.path.read_text())
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(record["salt"]), n=16384, r=8, p=1
        )
        if (username != "admin" and account is None) or not hmac.compare_digest(digest, base64.b64decode(record["hash"])):
            raise ValueError("Неверный пароль")
        session = secrets.token_urlsafe(32)
        with self.lock:
            self.attempts.pop(client, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > time.time()}
            self.sessions[hashlib.sha256(session.encode()).hexdigest()] = {'expires':time.time()+8*3600,'identity':identity}
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
