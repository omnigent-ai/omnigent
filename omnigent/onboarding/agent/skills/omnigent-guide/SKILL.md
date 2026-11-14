---
name: omnigent-guide
description: Use when the user asks about Omnigent itself or this machine's Omnigent setup, including install, CLI, harnesses, agent configs, skills, MCP servers, policies, sandboxes, automations, updates, or what you can do here. Skip it for questions about another tool on its own, or for general coding.
---

# Omnigent guide

You are running inside Omnigent, an open-source meta-harness: one
orchestration layer over Claude Code, Codex, Cursor, OpenCode, Hermes, Pi,
and agents defined in YAML. Use this skill to answer questions about the
Omnigent installed here. Do not answer from memory. Omnigent changes quickly,
and the installed version may differ from what you remember.

## Find the answer

Go to the source that fits the question. Run commands and fetch pages with
your shell tool (for example `sys_os_shell`).

1. **Commands, flags, and the version.** Run `omnigent --version` or
   `omnigent <command> --help`. These always match the installed build.
2. **This machine and this session.** Call the Omnigent tools, when your tool
   list has them:
   - `sys_session_get_info`: this session, its model, and the harnesses
     that are ready on this machine. Use it for "what can I run here".
   - `sys_agent_list` and `sys_agent_get`: the agents you can start.
   - `sys_policy_registry`: policy templates. `sys_add_policy` attaches one
     to this session.
   - `sys_scheduled_task_create`: creates an automation.
3. **Agent config, policies, and other features.** Read the docs for the
   installed version:
   `https://raw.githubusercontent.com/omnigent-ai/omnigent/v<version>/<path>`,
   with `<version>` from `omnigent --version` and `<path>` from the docs map
   below. If the tag does not exist (a source or dev build such as
   `0.19.0.dev0`), use `main` in place of `v<version>`.
4. **Anything else.** `https://omnigent.ai/llms.txt` lists every docs page;
   fetch the page you need. The site describes the latest release, so compare
   it with the installed version. Release notes are at
   `https://omnigent.ai/releases/<version>`.

Tell the user which source the answer came from. When sources disagree,
trust them in the order above. If none of them answers the question, say so
and point the user to https://omnigent.ai/docs.

## Core concepts

- **Harness**: the agent runtime. Native harnesses run a vendor CLI in a
  terminal (Claude Code, Codex, Pi, and others). SDK harnesses such as
  `claude-sdk`, `codex`, and `openai-agents` run inside the Omnigent runner.
- **Agent**: a YAML spec. It is either one `.yaml` file or a directory
  bundle with `config.yaml` and optional `AGENTS.md`, `skills/`, `tools/`,
  and `agents/`. Start one with `omnigent run <path>`.
- **Session**: one conversation with an agent. It follows the user across
  the terminal, the browser, a phone, and the desktop app.
- **Server, host, runner**: the server stores sessions and serves the web
  UI. A host is a machine that runs agents for a server. A runner runs
  sessions on a host.
- **Skills**: `skills/<name>/SKILL.md` files that an agent loads on demand.
- **Policies**: rules that check requests, responses, tool calls, and tool
  results, and allow, ask, or deny. In a bundle's `config.yaml` they go
  under `guardrails:` as `policies:`. A single-file agent YAML puts
  `policies:` at the top level.
- **Sandbox**: `os_env.sandbox` in the agent spec for a local OS sandbox,
  or `omnigent sandbox` for a remote one.
- **Automations**: scheduled tasks that run an agent on an RRULE schedule.
- **Data**: `~/.omnigent`. `OMNIGENT_DATA_DIR` moves it.

## Common tasks

| Task | Start with |
| --- | --- |
| First-time setup and credentials | `omnigent setup`, `omnigent config` |
| Start a session | `omnigent run <agent>`, or a harness command such as `omnigent claude`, `omnigent codex`, or `omnigent pi` |
| Run Omnigent in the background | `omnigent start`, `omnigent stop` |
| Serve the web UI or connect machines | `omnigent server`, `omnigent host`, `omnigent login` |
| Resume or manage sessions | `omnigent resume`, `omnigent session` |
| Keep agents in the new-session picker | `omnigent agent` |
| Report a problem | `omnigent diagnose` (a read-only snapshot), `omnigent doctor` |
| See usage and costs | `omnigent usage` |
| Update or remove Omnigent | `omnigent upgrade`, `omnigent uninstall` |
| Write a new agent | Load the `build-omnigent` skill. |

## Docs map

Paths are relative to the repository root (step 3).

| Topic | Path |
| --- | --- |
| Agent YAML fields: executor, tools, MCP servers, sub-agents, `os_env`, sandbox | `docs/AGENT_YAML_SPEC.md` |
| Agent directory bundles: `config.yaml`, `AGENTS.md`, skills, tools | `omnigent/spec/AGENTSPEC.md` |
| Policies | `docs/POLICIES.md` |
| Automations | `docs/AUTOMATIONS.md` |
| Where Omnigent stores data | `docs/DATA_DIR_LAYOUT.md` |
| Git providers and pull requests | `docs/git-providers.md`, `docs/git-pull-requests.md` |
| Azure DevOps, GitLab, GitHub App | `docs/AZURE_DEVOPS.md`, `docs/GITLAB.md`, `docs/GITHUB_APP_SETUP.md` |
| Databricks | `docs/databricks.md` |
| Long sessions and compaction | `docs/session-compaction.md` |
| Harness problems | `docs/harness-diagnostics.md` |
| Extensions and sandbox providers | `docs/extending/extensions.md`, `docs/extending/sandbox_providers.md` |
| Install, upgrade, deploy, remote access | `README.md`, `deploy/README.md` |

A running Omnigent server also serves its HTTP API reference at `/docs`
and `/openapi.json`.
