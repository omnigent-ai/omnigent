"""Tools for creating and removing git worktrees of the session's repository.

An agent that needs a second working tree — one per fan-out task, or a clean
tree to build in — would otherwise shell out to ``git worktree add`` and invent
a directory, which is how a repo ends up with worktrees scattered across four
different layouts and the project's setup script never running in any of them.
These tools route the same operation through Omnigent, so the worktree lands
under the project's configured root and the project's setup / teardown scripts
run around it.

Schema-only: the runner dispatches both to session-scoped server routes (see
``omnigent/server/routes/sessions/routes_worktrees.py``), which take the
repository from the calling session rather than from an argument.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysWorktreeCreateTool(Tool):
    """``sys_worktree_create`` — branch the session's repo into a new worktree."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_worktree_create"``."""
        return "sys_worktree_create"

    @classmethod
    def description(cls) -> str:
        """:returns: Description shown to the LLM."""
        return (
            "Create a git worktree of your session's repository, on a new "
            "branch. Use this instead of running 'git worktree add' yourself: "
            "it places the worktree where this project configures worktrees to "
            "go (so every tool and agent uses one location) and runs the "
            "project's setup script in it — dependency install, .env copy — "
            "before you use it. By default the new worktree forks from YOUR "
            "session's own branch, so work you already have in your worktree "
            "is the base every worktree you fan out builds on; pass "
            "base_branch only to fork from somewhere else. Returns the "
            "absolute worktree_path to work in, the branch, the base_branch it "
            "forked from, and setup: the script's exit code and output tail, or "
            "null when the project configures no setup script. If setup.ok is "
            "false the tree exists but is not prepared — report that rather "
            "than debugging the repo. Remove it with sys_worktree_remove when "
            "the work is done and its branch is pushed."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        :returns: OpenAI Chat-Completions tool schema for
            ``sys_worktree_create``.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "branch_name": {
                            "type": "string",
                            "description": (
                                "New branch to create and check out in the "
                                "worktree, e.g. 'feature/auth-refactor'. Must "
                                "be a valid git branch name and must not "
                                "already exist."
                            ),
                        },
                        "base_branch": {
                            "type": "string",
                            "description": (
                                "Optional ref to fork from, e.g. 'main' or "
                                "'origin/main'. Omit to fork from your own "
                                "session's branch, which is almost always "
                                "what you want — it keeps every worktree you "
                                "create anchored on the tree you are working "
                                "in."
                            ),
                        },
                    },
                    "required": ["branch_name"],
                    "additionalProperties": False,
                },
            },
        }


class SysWorktreeRemoveTool(Tool):
    """``sys_worktree_remove`` — tear down a worktree of the session's repo."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_worktree_remove"``."""
        return "sys_worktree_remove"

    @classmethod
    def description(cls) -> str:
        """:returns: Description shown to the LLM."""
        return (
            "Remove a git worktree of your session's repository, running the "
            "project's teardown script in it first (stop a dev server, drop a "
            "database). Use this instead of 'git worktree remove'. Only a "
            "worktree of this session's own repository can be removed, never "
            "the main checkout and never the tree you are running in. By "
            "default the branch survives, so unpushed work is recoverable. "
            "delete_branch is refused unless that branch's commits are already "
            "in YOUR branch — integrate its work first, or leave the branch. "
            "Returns the teardown script's result, or null when the project "
            "configures none."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        :returns: OpenAI Chat-Completions tool schema for
            ``sys_worktree_remove``.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "worktree_path": {
                            "type": "string",
                            "description": (
                                "The worktree to remove, as returned by "
                                "sys_worktree_create, e.g. "
                                "'/home/me/repo/.worktrees/feature-auth'."
                            ),
                        },
                        "delete_branch": {
                            "type": "boolean",
                            "description": (
                                "Also delete the branch checked out there. "
                                "Defaults to false. Refused unless the "
                                "branch's commits are already reachable from "
                                "your own branch, so it cannot destroy work "
                                "you have not integrated."
                            ),
                        },
                    },
                    "required": ["worktree_path"],
                    "additionalProperties": False,
                },
            },
        }
