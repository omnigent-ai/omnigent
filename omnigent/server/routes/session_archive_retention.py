"""Configure and preview inactive-session archive retention.

``GET /v1/session-archive-retention`` returns the caller's policy. It is
disabled until configured, and the protection rules are included before it is
turned on. ``PUT`` updates the period and protections. ``POST .../preview``
is a dry run. ``POST .../run`` archives eligible sessions the caller owns and
records the run on the policy.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    SessionArchiveRetentionPolicyResponse,
    SessionArchiveRetentionPreviewRequest,
    SessionArchiveRetentionPreviewResponse,
    SessionArchiveRetentionProtection,
    SessionArchiveRetentionRules,
    SessionArchiveRetentionRunRecord,
    SessionArchiveRetentionRunResponse,
    SessionArchiveRetentionSession,
    UpdateSessionArchiveRetentionRequest,
)
from omnigent.server.session_archive_retention import (
    RetentionDecision,
    SessionArchiveRetentionPolicy,
    SessionArchiveRetentionService,
    protection_rules,
    validate_protect_label_keys,
)


def _rules_model(policy: SessionArchiveRetentionPolicy) -> SessionArchiveRetentionRules:
    raw = protection_rules(policy)
    return SessionArchiveRetentionRules(
        inactivity_basis=raw["inactivity_basis"],
        always_exclude=raw["always_exclude"],
        protections=[
            SessionArchiveRetentionProtection.model_validate(item) for item in raw["protections"]
        ],
    )


def _session_model(decision: RetentionDecision) -> SessionArchiveRetentionSession:
    return SessionArchiveRetentionSession(
        id=decision.candidate.id,
        title=decision.candidate.title,
        updated_at=decision.candidate.updated_at,
        reasons=list(decision.reasons),
    )


def _policy_response(
    policy: SessionArchiveRetentionPolicy,
) -> SessionArchiveRetentionPolicyResponse:
    last_run = None
    if policy.last_run is not None:
        last_run = SessionArchiveRetentionRunRecord.model_validate(policy.last_run)
    return SessionArchiveRetentionPolicyResponse(
        enabled=policy.enabled,
        inactive_days=policy.inactive_days,
        protect_pinned=policy.protect_pinned,
        protect_shared=policy.protect_shared,
        protect_project=policy.protect_project,
        protect_label_keys=list(policy.protect_label_keys),
        rules=_rules_model(policy),
        last_run=last_run,
    )


def create_session_archive_retention_router(
    service: SessionArchiveRetentionService,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the inactive-session retention router (mounted under ``/v1``)."""
    router = APIRouter()

    @router.get(
        "/session-archive-retention",
        response_model=SessionArchiveRetentionPolicyResponse,
    )
    async def get_session_archive_retention(
        request: Request,
    ) -> SessionArchiveRetentionPolicyResponse:
        """Return the caller's retention policy and the rules it will apply."""
        user_id = require_user(request, auth_provider)
        policy = await asyncio.to_thread(service.get_policy, user_id)
        return _policy_response(policy)

    @router.put(
        "/session-archive-retention",
        response_model=SessionArchiveRetentionPolicyResponse,
    )
    async def put_session_archive_retention(
        request: Request,
        body: UpdateSessionArchiveRetentionRequest,
    ) -> SessionArchiveRetentionPolicyResponse:
        """Configure, disable, or update the inactivity period and protections."""
        user_id = require_user(request, auth_provider)
        try:
            label_keys = validate_protect_label_keys(body.protect_label_keys)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        saved = await asyncio.to_thread(
            service.save_policy,
            user_id,
            SessionArchiveRetentionPolicy(
                enabled=body.enabled,
                inactive_days=body.inactive_days,
                protect_pinned=body.protect_pinned,
                protect_shared=body.protect_shared,
                protect_project=body.protect_project,
                protect_label_keys=label_keys,
            ),
        )
        return _policy_response(saved)

    @router.post(
        "/session-archive-retention/preview",
        response_model=SessionArchiveRetentionPreviewResponse,
    )
    async def preview_session_archive_retention(
        request: Request,
        body: SessionArchiveRetentionPreviewRequest | None = None,
    ) -> SessionArchiveRetentionPreviewResponse:
        """Dry-run the policy. No session is archived."""
        user_id = require_user(request, auth_provider)
        override = None if body is None else body.inactive_days
        policy, cutoff, decisions, truncated = await asyncio.to_thread(
            service.preview,
            user_id,
            inactive_days=override,
        )
        would_archive = [
            _session_model(decision) for decision in decisions if not decision.reasons
        ]
        protected = [_session_model(decision) for decision in decisions if decision.reasons]
        return SessionArchiveRetentionPreviewResponse(
            enabled=policy.enabled,
            inactive_days=policy.inactive_days,
            cutoff=cutoff,
            would_archive=would_archive,
            protected=protected,
            truncated=truncated,
            rules=_rules_model(policy),
        )

    @router.post(
        "/session-archive-retention/run",
        response_model=SessionArchiveRetentionRunResponse,
    )
    async def run_session_archive_retention(
        request: Request,
    ) -> SessionArchiveRetentionRunResponse:
        """Archive eligible inactive sessions and record the policy action."""
        user_id = require_user(request, auth_provider)
        result = await asyncio.to_thread(service.apply, user_id)
        return SessionArchiveRetentionRunResponse(
            applied=result.applied,
            archived_session_ids=list(result.archived_ids),
            skipped=[_session_model(decision) for decision in result.skipped],
            truncated=result.truncated,
            ran_at=result.ran_at,
        )

    return router
