# Offline agent-image validation

`omnigent validate` checks a supported local agent image as data, without
starting agents, importing bundle-local Python or policy handlers, resolving
credentials, expanding environment variables, contacting services, or changing
the input. It also bypasses normal CLI state migration, diagnostic logs, update
checks, and community-plugin loading. Operator-required CLI wrappers still apply.
For this command, the working directory and supplied bundle roots are removed
from dependency lookup before package initialization, so a bundle-local
`yaml.py` or `hashlib.py` cannot replace an installed dependency.

This is an independently useful **static preflight**, not proof that an agent
can run. It does not validate deployment readiness or execute workflows.

## Try it

Save this as `my-agent/config.yaml`:

```yaml
spec_version: 1
name: example
instructions: You are a helpful assistant.
executor:
  config:
    harness: claude-sdk
  auth:
    type: api_key
    api_key: ${MY_API_KEY}
```

On a supported Omnigent installation (Linux or macOS):

```bash
omnigent validate my-agent --offline --json
omnigent validate my-agent/config.yaml --json
```

Both commands return the same JSON and exit `0`; `MY_API_KEY` need not exist.
Change `spec_version` to `2` and the result becomes `invalid`, with exit `1`.
Pass a nonexistent path or an unknown CLI option to get exit `2`.

`omni` and `python -m omnigent` use the same entry point. The path defaults to
the current directory; offline mode is always enabled, so `--offline` is
optional. Without `--json`, output is human-readable and still lists skipped
checks. Runtime root flags such as `--profiling`, `--debug`, and
`--log-to-stderr` are not supported for `validate`; they are rejected instead
of activating their startup effects. `validate --help` exits `0`.

## Supported scope

The schema remains the existing
[Agent Image Spec](../omnigent/spec/AGENTSPEC.md), not a separate YAML format.
Inputs are a directory containing `config.yaml`, or a `.yaml`/`.yml` file
containing `spec_version: 1`. An explicit file uses its parent directory as its
asset root, just as `config.yaml` does.

| Area | Offline checks |
| --- | --- |
| Config | Version, names, descriptions, boolean capability flags, sharing policy, interaction modalities and compaction; existing parser and static validator rules apply. |
| Executor | `type`, `model`, `reasoning_effort`, `profile`, `connection`, `auth`, `timeout`, `max_iterations`, `context_window`; `config` supports `harness` and `profile`. Harness names come only from shipped metadata, not community plugins. |
| LLM | `model`, `profile`, `connection`, `request_timeout`, `retry`. Additional provider-specific options are outside this initial scope. |
| Tools | Declared sub-agent references, timeout/retry, bare configurable builtin names, HTTP/stdio inline MCP declarations and `tools/mcp/*.yaml` declarations; required fields, transport conflicts and name collisions. |
| Local code | Names of `tools/python/*.py` and `tools/typescript/*.ts` are checked for collisions. Their contents are not read, parsed, imported or executed. |
| Sub-agents | Recursive `agents/<directory>/config.yaml` images. Missing or invalid children are errors, never pruned. Declared names, not directory names, are used for references. |
| Skills | Existing skill-directory and namespace layout, required frontmatter, name/description rules, `user-invocable` boolean and bundled-skill filter references. Other frontmatter fields are outside this scope. Skill body prose is opaque data. |
| Instructions | Inline text or contained files; the existing `AGENTS.md`, `CLAUDE.md`, `.cursorrules` fallback order applies. Single-line `.md`, `.txt`, `.rst` references must exist. Absolute, parent-traversing, home-relative and linked paths are rejected, not expanded. |
| Policies | Existing `guardrails` function-policy declarations, dotted reference syntax, label definitions, conditions, writable labels and positive ask timeouts. Each policy must declare exactly one of `function` or `handler`. Factory arguments and config must be mappings but their handler-specific semantics are not checked. |
| Opaque data | `params`, instruction text, skill bodies, and policy argument/config values are data, not programs or templates. |

Policy condition keys and writable labels must be declared in the same agent.
Condition values must belong to a label's declared `values` when present.
The ignored function-policy `on` selector and unknown policy fields are rejected
rather than giving a misleading impression of enforcement. Policy handlers,
including built-in ones, are never resolved: missing modules, factory signatures,
registry authorization and actual policy decisions remain runtime checks.

Unsupported content fails with exit `1`, not a successful partial result.
Examples include the standalone `name`/`prompt` YAML format without
`spec_version`, `os_env`, `terminals`, workflow declarations, provider-specific
LLM options, configured builtin mappings, and non-MCP inline tool declarations.
These need additional data-only adapters before offline support can be added.
Do not convert a standalone YAML by merely adding `spec_version`: its tool and
executor conventions differ.

Unknown static fields and shapes that the runtime parser would silently discard
or coerce are rejected. This deliberately makes the supported offline subset
stricter than runtime parsing. Environment references in string-valued connection,
auth, header and environment mappings are kept literal. Optional Hindsight tool
names are reserved for deterministic collision checks regardless of installation.
MCP files and skill frontmatter retain the runtime's YAML 1.1 scalar semantics;
quote string values such as `"on"` and `"yes"` in those assets. Config files
retain the existing config loader's narrower boolean rules.

Other files outside the agent-image discovery layout are not inspected. Tarballs,
remote URLs, UNC paths and stdin are not supported inputs. YAML must be a single
document, with string mapping keys and no duplicates, aliases, merge keys or
custom object tags. Symlinks, reparse points and special files on checked paths
are rejected. Limits are 1 MiB per read file, 1,000 read files/discovered directory
entries, 32 levels of YAML/agent nesting and 50,000 YAML nodes per document.
Use a stable local directory; concurrent filesystem replacement is not supported.

## Output and exit codes

| Exit | Meaning |
| --- | --- |
| `0` | All supported static checks passed (or help was requested). Runtime checks remain skipped. |
| `1` | Invalid or unsupported content, missing required assets, unreadable bundle content, or input limits exceeded. |
| `2` | Invalid invocation: unknown/extra arguments, nonexistent or unsupported top-level path, remote/archive input, unsafe top-level link, or required wrapper missing. |

With `--json`, stdout contains one JSON object, including for invocation errors.
Stderr is empty for these expected outcomes. For example, an invalid version
produces this diagnostic in the envelope:

```json
{
  "code": "INVALID_SPEC",
  "column": null,
  "field": "spec_version",
  "file": "config.yaml",
  "line": null,
  "message": "Only AgentSpec version 1 is supported.",
  "severity": "error"
}
```

The complete envelope has `schema_version: 1`, `mode: "offline"`, `status`
(`valid`, `invalid`, or `invalid_invocation`), `exit_code`, `diagnostics`, and
`skipped_checks`. Skipped check IDs are `HOST_AUTH`, `LIVE_SERVICES`, `CODE`,
and `PLUGINS`; each includes an explanation. Consumers should use version,
status, exit code and diagnostic codes rather than parse message text.

Diagnostics sort by bundle-relative file, field, line, column and code; JSON
object keys are sorted. There are no timestamps, durations or absolute host
paths. YAML syntax errors carry 1-based coordinates when available. Other
diagnostics identify safe field groups or numeric policy indexes. Parser
exceptions and validator messages can contain secrets, so neither raw messages
nor authored names/values are echoed or logged. This trades some error detail
for non-disclosure; nested validator errors may identify the sub-agent group
rather than its authored name. Bundle-relative filenames are visible.

Structural parsing stops at the first error, in deterministic discovery order;
once parsing succeeds, static validator errors are reported together. An
`unsupported` diagnostic always makes the overall result invalid.

## Contributor tests

In the repository's supported Linux/macOS development environment:

```bash
uv run --no-sync pytest tests/spec/test_offline.py tests/cli/test_cli_validate.py \
  tests/spec/test_parser.py tests/spec/test_policy_parser.py \
  tests/spec/test_validator.py tests/spec/test_policy_validator.py \
  tests/spec/test_load.py tests/cli/test_cli.py tests/cli/test_cli_invocation.py \
  tests/tools/builtins/test_registry_unified.py tests/test_harness_plugins.py -q
uv run --no-sync pytest -o addopts= tests/e2e/test_offline_validate_e2e.py -q
```

The e2e is intentionally credential-free: it runs the real module CLI in a fresh
process, validates an image containing child agents, skills, MCP declarations,
tool code and a policy factory, rejects runtime imports and network/process
attempts with tripwires, compares repeated JSON output and checks unchanged
input bytes. It does not require `--llm-api-key`, a running server or a live LLM.
