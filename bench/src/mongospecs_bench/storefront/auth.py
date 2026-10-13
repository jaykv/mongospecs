"""Stateless bearer tokens and password hashing, standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import typing as t
from dataclasses import dataclass

from mongospecs_web import Request, Unauthorized


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    is_admin: bool


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**12, r=8, p=1, dklen=32)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored.encode())
    except ValueError:
        return False
    salt, digest = raw[:16], raw[16:]
    candidate = hashlib.scrypt(password.encode(), salt=salt, n=2**12, r=8, p=1, dklen=32)
    return hmac.compare_digest(candidate, digest)


class Tokens:
    """``<user id>.<a|u>.<hmac>``: nothing to look up, nothing to store."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, user_id: t.Any, *, is_admin: bool = False) -> str:
        payload = f"{user_id}.{'a' if is_admin else 'u'}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> Principal:
        user_id, _, rest = token.partition(".")
        role, _, signature = rest.partition(".")
        if (
            not user_id
            or role not in ("a", "u")
            or not hmac.compare_digest(signature, self._sign(f"{user_id}.{role}"))
        ):
            raise Unauthorized("Invalid token.", headers={"www-authenticate": "Bearer"})
        return Principal(user_id, role == "a")

    def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized("Use a Bearer token.", headers={"www-authenticate": "Bearer"})
        return self.verify(token)
