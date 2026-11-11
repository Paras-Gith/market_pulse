import hashlib

import bcrypt


def _password_bytes(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> bytes:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt())


def verify_password(password: str, password_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash)
    except ValueError:
        return False
