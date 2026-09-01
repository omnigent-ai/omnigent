from __future__ import annotations

import hashlib
import hmac


class QmRequestSigner:
    def __init__(self, key_material: str) -> None:
        if len(key_material.strip()) < 32:
            raise ValueError("qm memory signing secret must be at least 32 characters")
        self._key = key_material.encode()

    def sign(self, *, timestamp: int, method: str, path: str, body: str) -> str:
        canonical = f"{method}\n{path}\n{body}"
        return hmac.new(
            self._key,
            f"v0:{timestamp}:{canonical}".encode(),
            hashlib.sha256,
        ).hexdigest()
