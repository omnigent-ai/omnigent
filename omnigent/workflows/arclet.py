"""Arclet workflow execution through Omnigent's managed Sessions API."""

from omnigent.workflows.engine import MemberRequest
from omnigent.workflows.models import ManagedSandbox
from omnigent.workflows.sessions import SessionExecutor


class ArcletExecutor(SessionExecutor):
    """One managed Arclet per node/member, retaining the session across owner rounds."""

    async def __call__(self, request: MemberRequest) -> str:
        placement = request.member.placement
        if not isinstance(placement, ManagedSandbox) or placement.provider != "arclet":
            raise ValueError("Arclet execution requires sandbox placement with provider: arclet")
        return await super().__call__(request)
