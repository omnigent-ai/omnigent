"""Example observer: metadata stays in the operator's existing server logs."""

from omnigent.extensions import ExtensionContext, ExtensionHooks, SessionStatusNotification


async def activate(context: ExtensionContext) -> ExtensionHooks:
    """Observe status edges; the existing policy engine still owns approvals."""
    context.logger.info("Incident response extension started (%s)", context.version)

    async def on_status(event: SessionStatusNotification) -> None:
        context.logger.info("Incident session %s became %s", event.session_id, event.status)

    async def shutdown() -> None:
        context.logger.info("Incident response extension stopped")

    return ExtensionHooks(on_session_status=on_status, shutdown=shutdown)
