"""Converters from SQLAlchemy rows to internal entity dataclasses."""

from __future__ import annotations

from omnigent.db.db_models import SqlAgent, SqlJob, SqlRun
from omnigent.entities import Agent, Job, Run


def sql_agent_to_entity(row: SqlAgent) -> Agent:
    """
    Convert a :class:`SqlAgent` ORM row to an :class:`Agent` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: An :class:`Agent` dataclass instance.
    """
    return Agent(
        id=row.id,
        created_at=row.created_at,
        name=row.name,
        bundle_location=row.bundle_location,
        version=row.version,
        description=row.description,
        updated_at=row.updated_at,
        session_id=row.session_id,
    )


def sql_job_to_entity(row: SqlJob) -> Job:
    """
    Convert a :class:`SqlJob` ORM row to a :class:`Job` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Job` dataclass instance.
    """
    return Job(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        name=row.name,
        graph=row.graph,
        narrative=row.narrative,
        agent_id=row.agent_id,
        harness_override=row.harness_override,
        model_override=row.model_override,
        created_by=row.created_by,
        schedule_config=row.schedule_config,
    )


def sql_run_to_entity(row: SqlRun) -> Run:
    """
    Convert a :class:`SqlRun` ORM row to a :class:`Run` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Run` dataclass instance.
    """
    return Run(
        id=row.id,
        job_id=row.job_id,
        session_id=row.session_id,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        error=row.error,
        created_by=row.created_by,
    )
