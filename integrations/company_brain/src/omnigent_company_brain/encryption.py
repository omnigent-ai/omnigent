from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_HKDF_SALT = b"agent-platform.secret-box"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def derive_encryption_key(material: str, *, key_id: str) -> bytes:
    if len(material.encode("utf-8")) < 32:
        raise ValueError("company brain encryption key material must be at least 32 bytes")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=f"company-brain.credentials.{key_id}.v1".encode(),
    ).derive(material.encode())


@dataclass(frozen=True, slots=True)
class CredentialCipher:
    key_id: str
    key: bytes

    @classmethod
    def from_material(cls, material: str, *, key_id: str = "primary") -> CredentialCipher:
        return cls(key_id=key_id, key=derive_encryption_key(material, key_id=key_id))

    def encrypt_json(self, value: dict[str, Any], *, workspace_id: int, connection_id: str) -> str:
        nonce = os.urandom(12)
        plaintext = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        ciphertext = AESGCM(self.key).encrypt(
            nonce,
            plaintext,
            self._aad(workspace_id, connection_id),
        )
        return f"v1:{self.key_id}:{_b64encode(nonce)}:{_b64encode(ciphertext)}"

    def decrypt_json(
        self,
        value: str,
        *,
        workspace_id: int,
        connection_id: str,
    ) -> dict[str, Any]:
        parts = value.split(":")
        if len(parts) != 4 or parts[0] != "v1" or parts[1] != self.key_id:
            raise ValueError("unsupported credential ciphertext version or key id")
        plaintext = AESGCM(self.key).decrypt(
            _b64decode(parts[2]),
            _b64decode(parts[3]),
            self._aad(workspace_id, connection_id),
        )
        decoded = json.loads(plaintext)
        if not isinstance(decoded, dict):
            raise ValueError("credential payload must be a JSON object")
        return decoded

    @staticmethod
    def _aad(workspace_id: int, connection_id: str) -> bytes:
        return f"company-brain:{workspace_id}:{connection_id}".encode()
