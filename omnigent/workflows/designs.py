"""Saved workflow designs and their persistent, private planning sessions."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from pydantic import BaseModel

from omnigent.db.utils import builtin_agent_id
from omnigent.install_ledger import atomic_write_json
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.workflows.models import Workflow


class SavedWorkflow(BaseModel):
    workflow_id: str
    root_session_id: str
    workflow: Workflow


class WorkflowDesigns:
    def __init__(
        self,
        directory: Path,
        conversations: ConversationStore,
        agents: AgentStore,
        permissions: PermissionStore | None,
    ) -> None:
        self.directory = directory
        self.conversations = conversations
        self.agents = agents
        self.permissions = permissions
        self._lock = asyncio.Lock()

    def _path(self, owner: str, workflow_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", workflow_id):
            raise ValueError("Invalid workflow ID")
        return self.directory / hashlib.sha256(owner.encode()).hexdigest() / f"{workflow_id}.json"

    def load(self, owner: str, workflow_id: str) -> SavedWorkflow:
        saved = SavedWorkflow.model_validate_json(self._path(owner, workflow_id).read_text())
        root = self.conversations.get_conversation(saved.root_session_id)
        grant = self.permissions.get(owner, saved.root_session_id) if self.permissions else None
        if root is None or grant is None or grant.level != LEVEL_OWNER:
            raise FileNotFoundError("Workflow root session is unavailable")
        return saved

    async def save(self, owner: str, workflow_id: str, workflow: Workflow) -> SavedWorkflow:
        # The prototype has one server process; serialize first saves and retries.
        if self.permissions is None:
            raise ValueError("Workflow roots require session permissions to be configured")
        async with self._lock:
            path = self._path(owner, workflow_id)
            if path.exists():
                saved = await asyncio.to_thread(self.load, owner, workflow_id)
                saved.workflow = workflow
                await asyncio.to_thread(atomic_write_json, path, saved.model_dump(mode="json"))
                return saved
            agent_id = builtin_agent_id(CODEX_NATIVE_AGENT_NAME)
            if await asyncio.to_thread(self.agents.get, agent_id) is None:
                raise ValueError("The built-in planning agent is unavailable")
            root = await asyncio.to_thread(
                self.conversations.create_conversation,
                agent_id=agent_id,
                title=workflow.name,
                labels={"omnigent.workflow.id": workflow_id, "omnigent.workflow.root": "true"},
            )
            try:
                await asyncio.to_thread(self.permissions.grant, owner, root.id, LEVEL_OWNER)
                saved = SavedWorkflow(
                    workflow_id=workflow_id, root_session_id=root.id, workflow=workflow
                )
                await asyncio.to_thread(atomic_write_json, path, saved.model_dump(mode="json"))
            except BaseException:
                await self.conversations.delete_conversation(root.id)
                raise
            return saved
