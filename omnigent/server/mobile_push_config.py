"""Explicit service-account configuration and signing, separate from HTTP sinks."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import jwt
from pydantic import BaseModel

from omnigent.server.feature_flags import Feature, FeatureFlags

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_logger = logging.getLogger(__name__)


class FcmInfo(BaseModel):
    enabled: bool
    project_id: str | None = None


class PushInfo(BaseModel):
    fcm: FcmInfo
    preview: bool = False


@dataclass(frozen=True)
class FcmConfig:
    project_id: str
    client_email: str = field(repr=False)
    private_key: str = field(repr=False)

    @classmethod
    def from_env(
        cls, flags: FeatureFlags, environ: Mapping[str, str] | None = None
    ) -> FcmConfig | None:
        source = os.environ if environ is None else environ
        path = source.get("OMNIGENT_FCM_CREDENTIALS_FILE", "").strip()
        if not flags.enabled(Feature.MOBILE_PUSH):
            if path:
                _logger.info("FCM credentials configured but mobile_push is disabled")
            return None
        if not path:
            _logger.warning("mobile_push is dormant: FCM credentials are not configured")
            return None
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("type") != "service_account":
                raise ValueError
            project = document.get("project_id")
            email = document.get("client_email")
            key = document.get("private_key")
            if not isinstance(project, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project
            ):
                raise ValueError
            if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
                raise ValueError
            if not isinstance(key, str):
                raise ValueError
            config = cls(project, email, key)
            config.assertion()
        except (OSError, ValueError, TypeError, jwt.PyJWTError):
            raise ValueError("Invalid FCM service-account credentials") from None
        return config

    def assertion(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.client_email,
                "scope": FCM_SCOPE,
                "aud": TOKEN_ENDPOINT,
                "iat": now,
                "exp": now + 3600,
            },
            self.private_key,
            algorithm="RS256",
        )

    @property
    def send_endpoint(self) -> str:
        return f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
