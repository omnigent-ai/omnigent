"""format_record tool (e2e fixture, pydantic BaseModel arg with optional field)."""

from __future__ import annotations

from omnigent_client.tools import tool
from pydantic import BaseModel


class PersonRecord(BaseModel):
    """A person record (test fixture)."""

    name: str
    age: int
    email: str | None = None


@tool
def format_record(record: PersonRecord) -> str:
    """
    Format a person record as a one-line string.

    :param record: The person record to format, e.g.
        ``PersonRecord(name="Bob", age=42)``.
    :returns: ``"Person(name=..., age=...[, email=...])"``, e.g.
        ``"Person(name=Bob, age=42)"``.
    """
    parts = [f"name={record.name}", f"age={record.age}"]
    if record.email is not None:
        parts.append(f"email={record.email}")
    return "Person(" + ", ".join(parts) + ")"
