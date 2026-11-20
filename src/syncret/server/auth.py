import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

# scrypt cost: about 64 MB and a fraction of a second per check
SCRYPT = {"n": 2**16, "r": 8, "p": 1}


# the folder for secrets inside the data volume, readable only by the server's user
def secrets_dir(data):
    folder = Path(data) / "secrets"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    return folder


# write a secret file atomically with mode 0600
def write_secret(path, text):
    partial = path.with_suffix(".partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.replace(partial, path)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=2**27, **SCRYPT)
    return {"salt": salt.hex(), "hash": digest.hex(), **SCRYPT}


def verify_password(password, record):
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]),
                            n=record["n"], r=record["r"], p=record["p"], maxmem=2**27)
    return hmac.compare_digest(digest.hex(), record["hash"])


def set_password(data, password):
    if len(password) < 12:
        raise ValueError("use a password of at least 12 characters")
    write_secret(secrets_dir(data) / "password.json", json.dumps(hash_password(password)))


def password_record(data):
    path = secrets_dir(data) / "password.json"
    return json.loads(path.read_text()) if path.is_file() else None


# the key that signs session cookies, made once per data folder
def session_key(data):
    path = secrets_dir(data) / "session.key"
    if not path.is_file():
        write_secret(path, secrets.token_hex(32))
    return path.read_text().strip()


# the csrf token for a session, made on first use
def csrf_token(session):
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_ok(session, token):
    expected = session.get("csrf")
    return bool(expected and token) and hmac.compare_digest(expected, token)
