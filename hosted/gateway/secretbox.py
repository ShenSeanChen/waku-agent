"""Encryption for a secret control.db has to keep (spec 026 C1).

control.db is copied off the VM every night (backup.sh), and config/ is not.
A Slack bot token is therefore stored sealed with a key that lives only in
config/gateway.env: a copy of the database, from a backup or from the disk,
holds no token that works.

AES-256-GCM from `cryptography`, which the hosted extra already installs for
PyJWT. Each value gets a new 12-byte nonce, and `bound_to` is authenticated
with it, so a sealed value moved to another row does not open there.

The key never appears in an error, a log line or a repr.
"""

from __future__ import annotations

import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
# The first byte of every sealed value. A second key or cipher gets a new one.
VERSION = b"\x01"
_HEX_KEY = re.compile(r"[0-9a-fA-F]{64}")
KEY_SHAPE = "the key must be 64 hexadecimal characters (32 bytes)"


class Unreadable(ValueError):
    """A sealed value did not open: another key, another row, or changed bytes."""


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(KEY_SHAPE)
        self._aead = AESGCM(key)

    @classmethod
    def from_hex(cls, text: str) -> SecretBox:
        """The key as install.sh writes it. The refusal never quotes it."""
        if not _HEX_KEY.fullmatch(text):
            raise ValueError(KEY_SHAPE)
        return cls(bytes.fromhex(text))

    def seal(self, plaintext: str, *, bound_to: str) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return VERSION + nonce + self._aead.encrypt(
            nonce, plaintext.encode("utf-8"), bound_to.encode("utf-8"))

    def open(self, sealed: bytes, *, bound_to: str) -> str:
        head = len(VERSION) + NONCE_BYTES
        if sealed[:1] != VERSION or len(sealed) <= head:
            raise Unreadable("not a sealed value")
        try:
            plain = self._aead.decrypt(sealed[1:head], sealed[head:],
                                       bound_to.encode("utf-8"))
        except InvalidTag:
            raise Unreadable("the sealed value did not open") from None
        return plain.decode("utf-8")
