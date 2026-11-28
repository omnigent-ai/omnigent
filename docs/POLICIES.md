# Policies

Policies are declarative gates that enforce rules on agent behavior. They evaluate agent actions at specific enforcement points and return one of three verdicts:

- **ALLOW** -- the action proceeds.
- **DENY** -- the action is blocked; the agent receives an error.
- **ASK** -- the action is paused for user approval; approved becomes ALLOW, refused becomes DENY.

Policies compose: multiple policies can be active at once. The engine evaluates them in declaration order. A DENY from any policy short-circuits the rest.

## Who configures policies

Policies are set at three levels. Each level serves a different persona:

| Level | Who | How | Evaluated |
|-------|-----|-----|-----------|
| **Server-wide** | Admin | `policies` in server config YAML, or REST API | Last |
| **Agent spec** | Agent developer | `policies` in agent YAML | Middle |
| **Session** | End user | Session settings panel in the UI | First |

Session policies evaluate first and can short-circuit (DENY) before spec or admin policies run.

![Policy trust model](images/policy-trust-model.png)

---

## For server admins

### Setting up server-wide policies

Server-wide (default) policies apply to every session. They act as organizational guardrails.

**1. Choose policies.** Browse the builtin policy registry (see [Builtin policies](#builtin-policies) below) or install community/custom policy modules.

**2. Register custom policy modules** (optional). If you use policies outside the builtins, add their module paths to the server config so they appear in the registry:

```yaml
# server_config.yaml
policy_modules:
  - myorg_policies
  - github_mcp_policy
```

**3. Add policies to the server config.**

```yaml
# server_config.yaml
policies:
  session_budget:
    type: function
    handler: omnigent.policies.builtins.cost.cost_budget
    factory_params:
      max_cost_usd: 10.00
      ask_thresholds_usd: [5.00]
  global_rate_limit:
    type: function
    handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
    factory_params:
      limit: 200
```

**4. Start the server.**

```bash
omnigent server --config server_config.yaml
```

After starting, you can also add or remove policies at runtime through the REST API (see [Admin policy REST API](#admin-policy-rest-api)).

### Attachment admission controls

The upload, copy, and fork routes apply additional server settings to files
delivered through the harness's filesystem tools: by default `.zip`, `.docx`,
`.xlsx`, `.pptx`, `.db`, `.sqlite`, `.sqlite3`, and `.mp4` files. These files require
an updated execution host/runner and a supported native coding agent for their
delivery. They are stored without extraction; the harness
reads a local copy outside the working checkout. See
[attached files and sandbox access](DATA_DIR_LAYOUT.md#attached-files).

Supported agents are Claude Code, Codex, Pi, Cursor, Kiro, Goose, Antigravity,
Qwen Code, Kimi, Hermes, and Devin. OpenCode and API/SDK harnesses do not support
filesystem attachments. Forking sent file history requires Claude Code or Codex.

| Server config key | Default | Scope |
|-------------------|---------|-------|
| `filesystem_attachment_allowed_extensions` | the eight extensions above | Which files are delivered as files (see below) |
| `filesystem_attachment_max_bytes` | `52428800` (50 MiB) | Bytes per file |
| `filesystem_attachment_max_files` | `20` | Stored files of these types per session |
| `filesystem_attachment_max_total_bytes` | `209715200` (200 MiB) | Combined bytes of these types per session |
| `filesystem_attachment_denied_extensions` | `[]` | Further restrict the allowlist, e.g. `[".zip", ".db"]` |

`filesystem_attachment_allowed_extensions` accepts:

- a list of extensions, which replaces the defaults, e.g. `[".zip", ".gz", ".har"]`.
  Files match on their last extension, so list `.gz` for `.tar.gz` files; a
  compound entry such as `.tar.gz` is skipped with a warning.
  A listed text or image type, such as `.log` or `.png`, is delivered as a file,
  with its original bytes, instead of inline. It then needs a supported native
  coding-agent session, like any other file delivered as a file.
  A default extension left off the list is refused, even when the browser
  labels it as text.
- `[]`, which turns filesystem attachments off.
- `"*"` (or a list containing `"*"`), which keeps the defaults and also delivers
  every other file that can't be inlined, such as `.gz`, `.tar`, `.har`, or
  other video formats.
  Images, PDFs, and text/code files stay inline, except for extensions listed
  beside `"*"`: `["*", ".log"]` also delivers `.log` files as files.

The server reads this key at startup. Each upload records how it is delivered,
so changing the setting does not change files a session already holds. MP4
videos and formats beyond the original archive, Office, and database extensions
need a host and runner that advertise
`filesystem_attachment_delivery`; older builds refuse them with an update
message. `GET /v1/info` reports the effective setting as
`filesystem_attachments`, which the web composer uses to accept files before
uploading them.

MP4 videos are delivered as files, without decoding or frame extraction. The
agent can inspect them with local tools such as `ffmpeg`; Claude Code's `Read`
tool cannot read the video directly.

These are filename-based admission and storage limits, not content inspection.
Classification uses the stored filename's extension, even when the browser
reports a different MIME type. It does not identify file formats from their
bytes, inspect archive entries, or detect renamed binary content. The denylist
does not restrict files the agent creates or downloads through other tools.
Images, PDFs, and text/code uploads retain their separate existing limits;
the web composer also has a fixed 50 MiB ceiling for files delivered as files.

Request-phase policies receive the filename and content type for these files,
with an empty `text` value. Their document, archive, and database contents are
not scanned as text. Ordinary text/code attachments continue to provide decoded
text to request-phase policies. No attachment delivery-mode field is added to
the policy payload.

---

## For agent developers

### Adding policies to an agent spec

Policies are declared under `policies` at the top level of the agent YAML. They are evaluated in declaration order.

```yaml
name: github_agent
prompt: You are a coding assistant with access to GitHub.

executor:
  harness: claude-sdk
  model: databricks-claude-sonnet-4-6

tools:
  github:
    type: mcp
    url: https://api.githubcopilot.com/mcp/

policies:
  limit_tool_calls:
    type: function
    handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
    factory_params:
      limit: 100
  github_access:
    type: function
    handler: omnigent.policies.builtins.github.github_policy
    factory_params:
      write_repos:
        - myorg/my-repo
      write_branches:
        - "feature/*"
  google_policy:
    type: function
    handler: omnigent.policies.builtins.google.gdrive_policy
    factory_params:
      read_all: true
      allow_create: true
```

### Policy declaration syntax

Each policy entry has:

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | `"function"` |
| `handler` | yes | Dotted Python import path to the callable or factory |
| `factory_params` | no | Key-value arguments passed to a factory at build time |

**Direct callable** (no parameters):

```yaml
approve_file_ops:
  type: function
  handler: omnigent.policies.builtins.safety.ask_on_os_tools
```

**Factory** (with parameters -- called once at build time to produce the evaluator):

```yaml
rate_limit:
  type: function
  handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
  factory_params:
    limit: 50
```

`factory_params` is equivalent to the inline `function: {path, arguments}`
form, so the block above is the same as:

```yaml
rate_limit:
  type: function
  function:
    path: omnigent.policies.builtins.safety.max_tool_calls_per_session
    arguments:
      limit: 50
```

Supply arguments through one of the two — declaring both `factory_params`
and `function.arguments` on the same policy is rejected.

---

## For session users

### Adding policies to a running session

Session-level policies let you customize agent behavior for your current task. There are two ways to add them:

1. **UI** -- Open the information window to browse available policies and toggle them on or off.
2. **Chat** -- Tell the agent directly, e.g. *"add a policy that asks me before running shell commands"*. The agent has a built-in `sys_add_policy` tool and will configure the policy for you.

Session policies evaluate before agent spec and admin policies, so they can enforce stricter rules or add additional gates for your specific workflow.

---

## Builtin policies

### Safety

#### `max_tool_calls_per_session`

Limits the total number of tool calls in a session. DENYs after the limit is reached.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `100` | Maximum tool calls allowed |

```yaml
rate_limit:
  type: function
  handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
  factory_params:
    limit: 50
```

#### `ask_on_os_tools`

Requires user approval before any `sys_os_read`, `sys_os_write`, `sys_os_edit`, or `sys_os_shell` tool call. No parameters (direct callable).

```yaml
approve_file_ops:
  type: function
  handler: omnigent.policies.builtins.safety.ask_on_os_tools
```

#### `block_skills`

Prevents the agent from loading specific skills.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `blocked` | string[] | (required) | Skill names to block (case-insensitive) |

```yaml
no_deploy_skill:
  type: function
  handler: omnigent.policies.builtins.safety.block_skills
  factory_params:
    blocked: [deploy, rollback]
```

#### `enforce_sandbox`

Forces a specific sandbox configuration on agent start.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sandbox_type` | string | `"linux_bwrap"` | Sandbox backend (`linux_bwrap`, `darwin_seatbelt`, `none`) |
| `allow_network` | boolean | `true` | Allow network access |
| `write_paths` | (string or object)[] | `null` | Writable paths; objects accept `path` and `copy_on_write` (null inherits agent config) |
| `read_paths` | string[] | `null` | Read-only paths (null inherits agent config) |
| `env_passthrough` | string[] | `null` | Env vars allowed through to the agent process (see below) |

`env_passthrough` is the escape hatch for an agent that authenticates from an
environment variable. Agent CLIs are spawned with a **deny-by-default**
environment: the shared base (`HOME`, `PATH`, proxy/TLS, locale, `TMPDIR`, the
Omnigent session marker) plus the harness's own variable family, and nothing
else. A host secret belonging to some other provider is not inherited.

That means an ambient key which used to reach the CLI implicitly now has to be
declared. Two cases worth knowing:

- **A generic ACP agent** (`type: acp`) gets no vendor family at all, because
  Omnigent cannot know which vendor an arbitrary agent belongs to. Gemini via
  ACP needs `env_passthrough: ["GEMINI_API_KEY"]`.
- **Goose** owns `GOOSE_*` only. Goose configured through Omnigent's
  provider/gateway routing is unaffected, but a goose set up against an ambient
  provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...) needs that name
  declared here.

An agent that fails during its `initialize` handshake for no obvious reason is
worth checking against this first; the ACP executor logs a hint pointing back at
this field.

> **How the start probe is evaluated.** `enforce_sandbox` runs when the runner
> replays your tool-call policies over a synthetic `sys_agent_start` probe at
> launch. Only a transform takes effect there: a policy that ALLOWs the probe
> and returns a replacement payload (preserving the `name` and `arguments`
> fields) reshapes the launch, chaining through later policies. A plain `DENY`
> or `ASK` verdict on this probe does **not** block agent start — it is logged
> and ignored, so a general-purpose tool allowlist or cost gate that happens to
> reject the `sys_agent_start` name will not stop the agent from launching. The
> runner only refuses to start (fails closed) when a policy cannot be evaluated
> at all — it raised, could not be resolved, or returned a transform that drops
> the probe's `name`/`arguments` shape — because then its intended sandbox
> transform is unknown. Do not rely on a tool-call `DENY` to gate which agents
> may launch; there is no agent-start policy phase today.

#### `deny_pii_in_llm_request`

Scans user messages and LLM prompts for PII patterns (SSN, credit card, email, phone).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pii_types` | string[] | `["ssn", "credit_card", "email", "phone"]` | PII categories to scan |
| `action` | string | `"DENY"` | Action when PII detected (`DENY` or `ASK`) |

### Cost

#### `cost_budget`

Gates a session on cumulative LLM spend, at the **request** phase (before the LLM turn, so text-only turns are budgeted too) and the **tool-call** phase. ASKs the first time spend crosses each soft warning threshold. At the hard limit it acts as a **downgrade gate**, not a hard stop: it DENYs (the whole turn on `request`, or each tool call on `tool_call`) only while the session is on an expensive model -- telling the user to switch to a cheaper one with `/model` -- and allows them again once the session has switched.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_cost_usd` | number | (required) | Hard spend limit in USD. Once reached, the turn / tool calls are blocked while the session is on an expensive model. |
| `ask_thresholds_usd` | number[] | `null` | Soft warning checkpoints that ASK the first time spend crosses each (each must be < `max_cost_usd`) |
| `expensive_models` | string[] | Fable + Opus + GPT-5 (excl. `-mini`/`-nano`) | Case-insensitive substring tokens for the model tiers blocked once over budget (e.g. `"opus"` matches any Opus deployment). The default's broad `gpt-5` token matches the whole GPT-5 family except the cheap `-mini`/`-nano` variants; an explicit list is matched literally with no exclusions. `[]` disables the hard limit, leaving only the soft thresholds. |

```yaml
budget:
  type: function
  handler: omnigent.policies.builtins.cost.cost_budget
  factory_params:
    max_cost_usd: 5.00
    ask_thresholds_usd: [1.00, 3.00]
    expensive_models: ["opus", "gpt-5"]
```

#### `user_daily_cost_budget`

Same ASK / downgrade-gate behavior as `cost_budget`, but the budget is the **session owner's cumulative spend across all their sessions for the current UTC day**. The soft-threshold approval is remembered per user+day, so an approved checkpoint won't re-prompt that user again today -- even from a different session. Useful as a server-wide per-user daily cap.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_cost_usd` | number | (required) | Hard daily limit in USD. Once the owner's spend for the day reaches it, the turn / tool calls are blocked while on an expensive model. |
| `ask_thresholds_usd` | number[] | `null` | Soft daily warning checkpoints that ASK the first time the owner's daily spend crosses each (each must be < `max_cost_usd`) |
| `expensive_models` | string[] | Fable + Opus + GPT-5 (excl. `-mini`/`-nano`) | Case-insensitive substring tokens for the model tiers blocked once over the daily budget. An explicit list is matched literally with no exclusions. `[]` disables the hard limit, leaving only the soft thresholds. |

```yaml
# server_config.yaml -- a per-user daily cap applied to every session
daily_budget:
  type: function
  handler: omnigent.policies.builtins.cost.user_daily_cost_budget
  factory_params:
    max_cost_usd: 25.00
    ask_thresholds_usd: [10.00, 20.00]
```

### GitHub

#### `github_policy`

Controls GitHub access across MCP tools and `git`/`gh` shell commands. Restricts reads to an allowlist (unless `read_all`) and writes to specific repos/branches. Shell commands with ambiguous targets return ASK.

Shell comments, here-documents (including here-strings), line continuations, and
command substitutions are not classified. Completed command segments before
and after these regions retain their policy decisions when the parser can
determine their boundaries, including commands surrounding a comment. Unsupported
syntax adds an ASK decision; DENY takes precedence over ASK, which takes
precedence over ALLOW. Substitutions inside double quotes
also require approval, as do ANSI-C (`$'...'`) and locale-specific (`$"..."`)
quoting. Ordinary single-quoted or escaped literal delimiters remain arguments.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `read_all` | boolean | `true` | Allow all reads |
| `read_repos` | string[] | `[]` | Repos readable when `read_all` is false (`owner/repo` or URLs) |
| `write_repos` | string[] | `[]` | Repos the agent may write to |
| `write_branches` | string[] | `[]` | Branches writable within allowed repos (empty = any) |
| `mcp_tool_prefixes` | string[] | `["mcp__github__", "github__"]` | Tool-name prefixes to match |
| `shell_tools` | string[] | `["sys_os_shell"]` | Shell tools whose commands are parsed for git/gh |

```yaml
github_access:
  type: function
  handler: omnigent.policies.builtins.github.github_policy
  factory_params:
    write_repos:
      - myorg/frontend
      - myorg/backend
    write_branches:
      - "feature/*"
      - "fix/*"
```

### GitLab

#### `gitlab_policy`

Controls supported Git downloads and pushes, plus a small set of issue and merge-request activities through `glab` shell commands. Configure which GitLab servers and projects an agent can read and write, and which branches may receive pushes or supported API deletions. Projects can have nested groups (`team/subgroup/project`). Paths match case-insensitively. The policy is opt-in and uses the existing ALLOW, DENY, and ASK approval flow.

| Automatically classified commands | Scope |
|-----------------------------------|-------|
| `glab issue list`, `view` | Read issue lists and individual issues |
| `glab mr list`, `view`, `diff` | Read merge requests and their changes |
| `glab issue create` | Create an issue with explicit `--title` and `--description` |
| `glab issue note` | Comment on a numeric issue ID with explicit `--message` |
| `glab mr create` / `new` | Create a same-project MR with explicit projects, branches, text, and safety overrides |
| `glab mr update` | Update a numeric MR's title, description, labels, draft/ready state, or target-branch metadata |
| `git clone`, `fetch`, `pull` | Download from an explicit HTTPS project URL |
| `git push` | Push individually named refs to an explicit HTTPS project URL |
| `glab api` | Classify the explicit REST routes and fields listed below |

The aliases `ls`, `show`, `new`, `comment`, and MR `accept` receive the same checks. For issue and MR commands, automatic decisions require `--repo` / `-R` to specify a full HTTPS project URL or a configured `host/group/project` selector. Views, diffs, and comments also require a numeric issue or merge-request ID. Missing selectors, bare project paths, positional URLs, unknown options, and interactive input return ASK. Common list filters, pagination, JSON output, view comments, issue labels, and confidentiality are supported; other options require approval.

Supported writes outside `write_repos` return DENY. Supported reads outside `read_repos` return DENY when `read_all=false`; otherwise supported reads on configured hosts are allowed. A bare allowlist entry applies to that project path across configured hosts; an HTTPS URL pins it to one server. Invalid configuration fails when the policy loads.

MR creation requires `--repo` / `-R` and `--head` / `-H` to identify the same explicit server and project, literal `--source-branch` / `-s` and `--target-branch` / `-b`, non-empty `--title` / `-t` and `--description` / `-d`, plus `--remove-source-branch=false` and `--allow-collaboration=false`. These overrides exclude project defaults that could enable deletion or grant additional source-branch permissions. `--yes`, draft/WIP state, and labels are supported. Missing inputs, a different or inferred head project, pushing/filling, auto-merge, branch creation, attachments, templates, and hidden target-project overrides require approval.

MR updates require one numeric ID, an explicit `--repo`, and at least one supported metadata change: title, description, labels/unlabels, draft/WIP/ready state, or `--target-branch` (no `-b` alias on update). Empty or editor text and file-based input require approval. Creating or updating MR metadata does not change branch contents, so `write_branches` does not restrict it, including selecting a target branch that cannot receive automatic pushes. Descriptions containing a line beginning with `/` after optional whitespace require approval because GitLab may interpret them as quick actions. The check conservatively covers code-block lines and unrecognized slash commands too.

MR merge/`accept` and rebase remain approval-gated. A numeric MR ID does not establish its target branch or its possibly separate fork's source project and branch. In glab 1.123.0, `merge --remove-source-branch=false` omits the API override, leaving the stored removal preference unresolved. Known forbidden merge projects are denied; rebase does not treat the MR's containing project as its source write destination. An explicit removal request on create or merge is denied by default once the configured project is established, even if the MR ID is omitted. `allow_destructive=true` changes that guard to approval, not automatic permission. Update's `--remove-source-branch` toggles stored state and therefore asks; `=false` is a no-op, not a way to disable an existing preference.

Custom API requests require one explicit `--hostname`, one endpoint rooted at `projects/:project`, and `--placeholder project=team/app`. The custom placeholder is a literal project path that glab encodes as one route segment; it does not use the current checkout. The API hostname must be a configured hostname without a scheme or port, matching the glab API flag. Use `--placeholder ref=feature/topic` with the branch-member route `repository/branches/:ref` for branch names containing slashes. Only these two whole-segment custom placeholder names are supported; built-in, repeated, partial, unused, or unresolved placeholders ask. Numeric project IDs and fully qualified endpoint URLs ask because they do not establish this explicit project contract. The existing percent-sign shell guard is unchanged, so encoded command text such as `team%2Fapp` still asks.

The following suffixes are supported below `projects/:project`, where `N` is a positive numeric issue/MR/note ID:

| API method and suffix | Decision scope |
|-----------------------|----------------|
| `GET` with no suffix | Read project metadata |
| `GET issues`, `issues/N`, `merge_requests`, `merge_requests/N` | Read lists and individual objects |
| `GET issues/N/notes`, `merge_requests/N/notes`, and a numeric note member | Read notes |
| `GET merge_requests/N/changes`, `merge_requests/N/diffs`, `merge_requests/N/raw_diffs` | Read MR changes |
| `GET repository/branches` and a literal branch member or `:ref` | Read branches |
| `POST issues` | Write explicit title/description, optional labels and typed confidentiality |
| `POST issues/N/notes`, `merge_requests/N/notes` | Write explicit body text |
| `POST merge_requests` | Same-project creation with explicit branches, title/description, and typed false removal/collaboration overrides |
| `PUT merge_requests/N` | Update title, description, labels/add_labels/remove_labels, or target-branch metadata; optional typed false removal assignment |
| `PUT merge_requests/N/merge`, `merge_requests/N/rebase` | Ask for unresolved branch effects; a known forbidden merge project is denied |
| `DELETE repository/branches/<branch>` or `:ref` | Deny by default; opt-in still checks writable project and exact `write_branches` |

Use `-f` / `--raw-field key=value` for literal string fields and `-F` / `--field key=true/false` for supported booleans. Raw `@example.txt` stays literal; other typed values, duplicate or structured keys, and unknown fields ask. Fields imply POST unless an explicit method is supplied; read filters therefore need `-X GET`. Supported read filters are `page`, `per_page`, `state`, `search`, `labels`, `source_branch`, `target_branch`, `order_by`, and `sort`. Conflicting method selectors ask. Output controls `--include`, `--silent`, and `--output json/ndjson` are supported.

API MR creation requires `-F remove_source_branch=false` and `-F allow_collaboration=false`. Omitting `target_project_id` selects the same source project on the GitLab API; project-ID overrides ask, even if claimed to refer to that project. API metadata writes ignore `write_branches` and ask for empty text, invalid literal branch names, and potential quick-action lines in descriptions or note bodies. API removal true (typed or literal raw `true`) is denied by default and asks with destructive opt-in; other untyped removal values ask. A typed false update assigns false, unlike the CLI update flag's false no-op. The API can send a false merge-removal override, but merge/rebase still require approval because their affected branches or source projects remain unresolved.

On explicitly configured API hosts, recognized project, issue, and MR object DELETEs are denied even with destructive opt-in and numeric or unresolved project selectors. Branch-member and merged-branch bulk deletion are denied by default before resolving the project. Bulk deletion still asks with opt-in; member deletion also asks for extra fields or branch names containing dots, whose format-suffix handling is not modelled. Headers, pagination, GraphQL, multipart forms, file/stdin bodies (`--input`), unknown routes/methods, and unmodelled fields require approval.

For example, this reads a pinned project without checkout inference or percent-encoded command text:

```sh
glab api projects/:project --hostname gitlab.example.com --placeholder project=team/app
```

Git transfers require a full HTTPS project URL; named remotes (`origin`), omitted destinations, SSH URLs, and local paths return ASK. Common clone branch/depth options, fetch depth/pruning options, pull `--ff-only` / `--no-rebase`, and push verbosity/upstream/atomic options are supported. Unknown options return ASK. Each option keeps its value, including values that resemble another option.

An ordinary push must name its destination fully: `git push https://gitlab.example.com/team/app HEAD:refs/heads/feature`. `refs/heads/feature` alone also works. Bare `feature`, `HEAD:feature`, and `HEAD:heads/feature` return ASK because their meaning can depend on existing refs or configuration. Every destination branch must match `write_branches` exactly, with case preserved; omit this setting or use an empty list to permit any branch in a writable project. Branch restrictions do not affect issues, tags, or MR metadata.

Recognized force pushes, tag pushes, and remote ref deletions return DENY by default, including when the remote is named or omitted. A valid explicit HTTPS authority outside the configured GitLab hosts returns ASK before these guards; malformed project paths on configured hosts still receive the guards. Authorities include their port, and URLs carrying credentials remain unresolved and receive the guards. Set `deny_force_push=false`, `deny_tag_push=false`, or `allow_destructive=true` to pass the corresponding operation through project and branch checks. Explicit tag refs (`refs/tags/v1`) use project permissions and the tag guard. Bulk pushes (`--all`, `--mirror`, `--prune`, `--tags`, `--follow-tags`) still require approval when their default guards are disabled, because the full destination set is unresolved. Shorthand `push URL tag v1` also requires approval when the tag guard is disabled. Dry runs receive the same checks as real pushes.

Git global options (including `-C` and `-c`), `git config`, remote configuration commands, clone configuration, custom transfer programs, recursive submodule options, additional repositories, and push options require approval. `--no-recurse-submodules` is supported; other recursion modes remain behind approval. Other merge-request actions, forks, unmodelled `glab api` requests (including reads), and other command families also require approval. Recognized direct `glab issue`, `mr`, and `repo` deletion actions with supported shell syntax return DENY even when `allow_destructive=true`; that setting does not permit deleting GitLab issues, merge requests, or projects. Local Git commands such as `status`, `diff`, `add`, `commit`, `branch`, and `tag` remain eligible for normal execution when invoked without Git global options. Their subcommand options are not inspected; this includes `git branch -u origin/main`, which changes local upstream configuration. Attaching another forge policy may still make Git transfers require approval.

Shell wrappers and chained commands use the shared POSIX shell parser with conservative approval guards for syntax that differs in Windows shells. Single quotes, backslashes, carets, percent signs, smart quotes, embedded `!`, shell expansion, unquoted shell redirection, unmodelled ASCII control characters (other than tabs/newlines), and detected CLI environment names require approval, including when they occur in literal issue text. The environment guard checks identifier boundaries in raw command segments and parsed token text for protected variable names (`HOME`, `PATH`, `XDG_CONFIG_HOME`, `XDG_CONFIG_DIRS`, GitLab host/configuration selectors, `GIT_*` / `GITLAB_*` / `GLAB_*` and `LD_*` / `DYLD_*` prefixes, standard HTTP/proxy variables in either case, TLS certificate selectors, and `SSH_ASKPASS`) before wrapper resolution or command classification, including names attached to short options. Names match case-insensitively across hosts to cover Windows lookup. This conservatively asks for `export GIT_CONFIG_COUNT`, `printf -v GIT_CONFIG_COUNT 1`, and even literal text such as `git commit -m HOME` or `git add git_helpers.py`; it does not interpret shell variable effects. Use plain double quotes for text containing spaces. Double-quoted literal `<` and `>` remain ordinary text. These guards run before command recognition and tokenization, including for commands without a literal GitLab executable name. Options following a shell command-string flag (`bash -c -- ...`, for example) require approval. Standalone shell negation (`!`) preserves the underlying command’s decision. DENY takes precedence over ASK across independently understood command segments. Unrecognized command forms mentioning `git` or `glab` require approval. Executable recognition ignores casing and `.exe` suffix casing. This is not a full cmd.exe or PowerShell parser. The policy inspects command text and assumes existing CLI configuration and executables are trusted. This includes configuration written through file tools and environment state established in earlier tool calls; arbitrary file writes and script effects are not simulated. Literal protected variable names in removals (`env -u`, `unset`) also require approval. It does not contact GitLab to verify project existence or resolve configuration. GitLab MCP tools, direct HTTP clients, and arbitrary scripts accessing GitLab are outside its coverage; use additional controls for those paths. GitLab account permissions still apply after policy approval.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hosts` | string[] | `["gitlab.com"]` | Trusted GitLab HTTPS authorities, including explicit ports |
| `read_all` | boolean | `true` | Allow supported reads across projects on configured hosts |
| `read_repos` | string[] | `[]` | Readable projects when `read_all=false` |
| `write_repos` | string[] | `[]` | Projects permitting supported issue/MR/API writes and Git pushes |
| `write_branches` | string[] | `[]` | Exact, case-sensitive push and API branch-deletion names; empty permits all |
| `allow_destructive` | boolean | `false` | Permit explicit Git ref and API branch deletions through write checks; MR source-removal requests still require approval |
| `deny_tag_push` | boolean | `true` | Block tag pushes |
| `deny_force_push` | boolean | `true` | Block force flags and force-prefixed refspecs |
| `shell_tools` | string[] | every harness shell tool | Tools whose `command` argument is inspected |
| `deny_reason` | string | `GitLab operation blocked by policy.` | Prefix for DENY explanations |

```yaml
gitlab_access:
  type: function
  handler: omnigent.policies.builtins.gitlab.gitlab_policy
  factory_params:
    hosts:
      - gitlab.example.com
    read_all: false
    read_repos:
      - https://gitlab.example.com/team/app
    write_repos:
      - https://gitlab.example.com/team/app
```

For this configuration, `glab issue create -R https://gitlab.example.com/team/app -t Fix -d Details` is allowed by this policy, the same write to `team/other` is denied, and `git push origin feature` or an incomplete `glab mr create` asks for approval. Add `write_branches: [feature]` to allow pushes such as `git push https://gitlab.example.com/team/app HEAD:refs/heads/feature` while blocking a push to `refs/heads/main`.

This explicit same-project MR creation is also allowed:

```sh
glab mr create -R https://gitlab.example.com/team/app -H https://gitlab.example.com/team/app -s feature -b main -t Fix -d Details --remove-source-branch=false --allow-collaboration=false
```

### Google Workspace

#### `gdrive_policy`

Controls Google Drive / Docs / Sheets / Slides access. Writes are restricted to files the agent created in the current session plus explicitly allowed files.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `read_all` | boolean | `true` | Allow all reads |
| `read_files` | string[] | `[]` | File IDs or URLs readable when `read_all` is false |
| `allow_create` | boolean | `false` | Allow creating new files |
| `write_files` | string[] | `[]` | File IDs or URLs always writable |
| `comment_files` | string[] | `[]` | File IDs or URLs the agent may comment on |
| `tool_prefixes` | string[] | `["mcp__google__", "google__"]` | Tool-name prefixes to match |

#### `gmail_policy`

Controls Gmail access. Defaults to read + draft but no send.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `allow_read` | boolean | `true` | Allow reading mail |
| `allow_send` | boolean | `false` | Allow sending mail |
| `allow_drafts` | boolean | `true` | Allow creating/editing own drafts |
| `allow_modify` | boolean | `false` | Allow modifying messages (labels, trash) |
| `tool_prefixes` | string[] | `["mcp__google__", "google__"]` | Tool-name prefixes |

#### `gcalendar_policy`

Controls Google Calendar access. Defaults to read-only.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `allow_read` | boolean | `true` | Allow reading calendars/events |
| `allow_create_events` | boolean | `false` | Allow creating events |
| `allow_modify_events` | boolean | `false` | Allow updating/deleting events |
| `tool_prefixes` | string[] | `["mcp__google__", "google__"]` | Tool-name prefixes |

### Working directory

#### `block_working_dir_changes`

Blocks shell commands that change the working directory (`cd`, `pushd`, `git -C`) or manage git worktrees. Parses chained and wrapped commands to prevent trivial bypasses.

Shell comments, here-documents (including here-strings), line continuations, and
command substitutions take the configured `action` because the parser cannot
classify them reliably. This also applies when the visible command does not
change directories. ANSI-C (`$'...'`) and locale-specific (`$"..."`) quoting also
take this action; ordinary single-quoted or escaped literal delimiters remain
arguments.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `block_cd` | boolean | `true` | Block cd/chdir/pushd/popd and git -C |
| `block_worktree` | boolean | `true` | Block git worktree add/move/remove |
| `allowed_dirs` | string[] | `[]` | Directories cd may move into (including subdirectories) |
| `action` | string | `"deny"` | Verdict for gated commands (`deny` or `ask`) |
| `shell_tools` | string[] | `["sys_os_shell"]` | Shell tools to parse |

### Risk score

#### `risk_score_policy`

Accrues a per-session risk score from tool calls and sensitive data labels. Escalates guarded tools to ASK or DENY once the score exceeds a threshold.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `threshold` | integer | `50` | Score at which guarded tools escalate |
| `tool_points` | object | `{}` | Tool name to points mapping, e.g. `{"web_search": 10}` |
| `sensitive_labels` | object | `{}` | Data-classification label to points, e.g. `{"Highly Confidential": 30}` |
| `guarded_tools` | string[] | `[]` | Tools gated once score reaches threshold |
| `escalate_action` | string | `"ASK"` | Verdict for guarded tools over threshold |
| `initial_scores_by_actor` | object | `{}` | Actor email to starting score offset |
| `state_key` | string | `"risk_score"` | Session state key for the running score |

### Routing

#### `deny_trivial_to_expensive_model`

Classifies user messages as TRIVIAL or COMPLEX using the server LLM. Denies trivial tasks from using expensive models. Requires the server to have an `llm:` config block.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `expensive_models` | string[] | (required) | Model IDs to gate, e.g. `["databricks-claude-opus-4-6"]` |
| `classification_prompt` | string | (builtin default) | System instructions for the classifier |

```yaml
# server_config.yaml
llm:
  model: databricks-gpt-5-4-mini

policies:
  deny_trivial_opus:
    type: function
    handler: omnigent.policies.builtins.routing.deny_trivial_to_expensive_model
    factory_params:
      expensive_models:
        - databricks-claude-opus-4-6
```

---

## Writing custom policies

### Policy function interface

A policy function receives an event dict and returns a response dict (or `None` to abstain).

```python
from omnigent.policies.schema import PolicyEvent, PolicyResponse

def my_policy(event: PolicyEvent) -> PolicyResponse | None:
    if event["type"] != "tool_call":
        return None  # abstain on non-tool phases
    tool = event["data"]["name"]
    if tool == "dangerous_tool":
        return {"result": "DENY", "reason": "This tool is blocked."}
    return {"result": "ALLOW"}
```

### Response segments and `turn_final`

`response` policies receive assistant text in `event["data"]`. In runner-relayed
sessions, they run before each nonempty text segment is persisted, including
progress text before tool calls. Use `event["context"]["turn_final"]` to decide
whether to perform completion-specific work:

| Value | Meaning |
|-------|---------|
| `True` | The final text segment of a successfully completed relayed turn. |
| `False` | An intermediate segment, or text from a failed, cancelled, or incomplete relayed turn. |
| `None` | The calling path does not distinguish segments. Also used outside the `response` phase. |

Within a response policy, skip only an explicit `False` to preserve existing
behavior on callers that supply `None`. `None` does not assert successful
completion. For example:

```python
from omnigent.policies.schema import PolicyEvent, PolicyResponse

def count_responses(event: PolicyEvent) -> PolicyResponse | None:
    if event["type"] != "response":
        return None
    if event.get("context", {}).get("turn_final") is False:
        return None
    return {
        "result": "ALLOW",
        "state_updates": [{"key": "responses", "action": "increment", "value": 1}],
    }
```

Content checks should inspect every segment, including those marked `False`.
The relay skips empty and whitespace-only segments: a turn that ends with a tool
call and no trailing text has no final response-policy invocation. `turn_final`
describes the current evaluation; it does not guarantee one callback per turn.

### Factory form

For policies that need configuration, write a factory -- a function that accepts parameters and returns the actual evaluator:

```python
def block_domains(blocked_domains: list[str]) -> callable:
    blocked = frozenset(d.lower() for d in blocked_domains)

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if event["type"] != "tool_call" or event["target"] != "web_fetch":
            return None
        url = event["data"]["arguments"].get("url", "")
        for domain in blocked:
            if domain in url.lower():
                return {"result": "DENY", "reason": f"Domain {domain} is blocked."}
        return {"result": "ALLOW"}

    return evaluate
```

### Event and response examples

**Event dict** passed to the policy callable (example for a `tool_call` phase):

```json
{
  "type": "tool_call",
  "target": "sys_os_shell",
  "data": {
    "name": "sys_os_shell",
    "arguments": {"command": "rm -rf /tmp/data"}
  },
  "context": {
    "actor": {"run_as": "alice@example.com", "client_id": "oauth_abc"},
    "usage": {
      "input_tokens": 1520,
      "output_tokens": 340,
      "total_tokens": 1860,
      "total_cost_usd": 0.012
    }
  },
  "session_state": {"call_count": 5},
  "request_data": null
}
```

**Response dict** returned by the policy callable:

```json
{
  "result": "DENY",
  "reason": "Destructive shell command blocked.",
  "state_updates": [
    {"key": "call_count", "action": "increment", "value": 1}
  ]
}
```

`result` is the only required field. Valid values: `"ALLOW"`, `"DENY"`, `"ASK"`. Return `None` to abstain.

`state_updates` supports four actions: `"set"`, `"increment"`, `"delete"`, `"append"`.

### Making policies discoverable

To make custom policies appear in the UI, export a `POLICY_REGISTRY` list from your module:

```python
# myorg/policies.py

POLICY_REGISTRY = [
    {
        "handler": "myorg.policies.block_domains",
        "kind": "factory",
        "name": "Block Domains",
        "description": "Block web access to specific domains.",
        "params_schema": {
            "type": "object",
            "properties": {
                "blocked_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Domains to block"
                }
            },
            "required": ["blocked_domains"]
        }
    }
]
```

Then register the module in the server config:

```yaml
policy_modules:
  - myorg.policies
```

---

## Appendix: Admin policy REST API

After starting the server, admins can manage default policies at runtime through these endpoints:

```bash
# Create a server-wide policy
curl -X POST http://localhost:6767/v1/policies \
  -H "Content-Type: application/json" \
  -d '{
    "name": "global_rate_limit",
    "type": "python",
    "handler": "omnigent.policies.builtins.safety.max_tool_calls_per_session",
    "factory_params": {"limit": 200}
  }'
```

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/policies` | Create a default policy |
| `GET` | `/v1/policies` | List all default policies |
| `GET` | `/v1/policies/{policy_id}` | Get a specific policy |
| `PATCH` | `/v1/policies/{policy_id}` | Update (name, handler, enabled) |
| `DELETE` | `/v1/policies/{policy_id}` | Remove a policy |

`GET /v1/policy-registry` lists all discoverable policies with parameter schemas -- useful for building admin UIs.
