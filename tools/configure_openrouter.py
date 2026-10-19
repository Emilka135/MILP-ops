"""Store the chosen model provider key locally without echoing it."""

import argparse
import os
import tempfile
from getpass import getpass
from pathlib import Path


def save_key(provider, key, root=None):
    if provider not in {"openrouter", "openai"}:
        raise ValueError("unsupported provider")
    key = key.strip()
    if not key or any(char in key for char in ("\n", "\r")):
        raise ValueError("empty or invalid key")
    folder = (root or Path(__file__).resolve().parents[1]) / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    # Write with private permissions from creation, including replacing an old key.
    fd, temporary = tempfile.mkstemp(prefix=".model-key-", dir=folder)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        destination = folder / (provider + ".key")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("openrouter", "openai"), default="openrouter")
    args = parser.parse_args()
    try:
        save_key(args.provider, getpass(args.provider + " API key (input hidden): "))
    except (ValueError, OSError):
        raise SystemExit("Key was not saved; check input and local filesystem permissions.") from None
    prefix = args.provider.upper()
    print("Saved locally in ignored secrets/" + args.provider + ".key.")
    print("Set AGENT_MODE=" + args.provider + " and " + prefix + "_MODEL in .env.")
    if args.provider == "openai":
        print("Set OPENAI_BASE_URL to your OpenAI-compatible gateway and OPENAI_API_KEY_FILE=/run/secrets/openai.key.")


if __name__ == "__main__":
    main()
