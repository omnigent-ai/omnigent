"""Server-issued credential purposes, independent of the user's permissions."""

from enum import StrEnum


class CredentialPurpose(StrEnum):
    """Issuance provenance, never proof of a person's approval of an operation."""

    SESSION = "session"
    CLIENT = "client"
    DELEGATED = "delegated"
    RUNNER = "runner"


CREDENTIAL_VERSION = 1
