"""Create the project-internal control key atomically, without logging its value."""
import os
from pathlib import Path
import secrets
import stat
import tempfile


def ensure_key(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        fd, temporary = tempfile.mkstemp(prefix=".control-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or len(stream.read(33)) != 32:
            raise ValueError("control key format or permissions invalid")
    return path


if __name__ == "__main__":
    ensure_key(os.environ.get("CONTROL_KEY_FILE", "/control/editor-rpc.key"))
    print("Internal editor control key ready", flush=True)
