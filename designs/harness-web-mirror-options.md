# PR 2.3 design options — driving the web off `/v1/harnesses`

Status: **Option 1 chosen** (user, 2026-08). 2.3a (data plumb) implemented; 2.3b
(presentation) pending. Community-provided icon resources deferred to a
follow-up (default generic icon for now). Feeds PR 2.3 of the modular
native-harness registry workstream.

## The question

`web/src/lib/nativeCodingAgents.ts` hardcodes an 11-row table of native coding
agents. PR 2.2 (#3672) now publishes native rows on `GET /v1/harnesses`
(`native_agents[]`) carrying `key`, `agent_name`, `harness`, `wrapper_label`,
`terminal_name`, `display_name`, `subagent_wrapper_label`, `fork_history`, and
the full `capabilities` dict. **2.3 replaces the literal with the endpoint —**
but four fields the web literal carries have *no clean server source today*, and
how we resolve each decides whether a community native harness can appear in the
UI at all. This doc grounds that decision in how the web actually consumes each
field.

## What the ~20 consumers actually need

I traced every importer of `nativeCodingAgents.ts`. The public API splits into
**four field groups** with very different portability:

### Group A — identity (already on the endpoint, trivially portable)
`key`, `agent_name`, `harness`, `wrapper_label`, `terminal_name`,
`display_name`, `subagent_wrapper_label`.
- Consumed by: `nativeCodingAgentForHarness/AgentName/Wrapper`,
  `isNativeTerminalSession`, `nativeWrapperLabelsForAgent`, plus the reversed
  `HARNESS_ALIASES` map (which mirrors `harness_aliases.NATIVE_HARNESSES`).
- **2.2 already emits all of these.** A community harness Just Works for
  recognition, wrapper-label stamping, and native-session detection.

### Group B — `fork_history` (already on the endpoint, replaces `forkHarness.ts` sets)
- `forkHarness.ts` hardcodes `NATIVE_REBUILD_HARNESSES` / `PREAMBLE_FORK_HARNESSES`
  (a `switch` over `claude-native` / `native-claude` / …). This is a *pure
  duplicate* of the server's `_FORK_HISTORY_NATIVE_HARNESSES` /
  `_CURSOR_FORK_HISTORY_HARNESSES`, which 1.8 already derives from the
  `fork_history` capability axis.
- **2.2 emits `fork_history` per row**, so the web `switch` becomes a lookup.
  Fully portable, no new decision.

### Group C — `sortRank` + marketing `displayName` (presentation, no server source)
- `sortRank`: picker ordering only (`agentGrouping.ts` sorts by it). Arbitrary
  10/20/25/… integers a human chose.
- `displayName`: the literal says **"Claude Code"** / **"Qwen Code"**; the
  registry `display_name` is **"Claude"** / **"Qwen"**. Marketing label vs
  identity label — genuinely different strings, used in the picker card title
  (`nativeDisplayNameForAgent`).
- Neither has a server source. Both are **pure presentation**.

### Group D — the capability picker list (`permissionMode`/`approvalMode`/`cursorMode`)
This is the one that looks like it should be a capability axis but **is not**:
- `NewChatDialog.tsx` gates three *distinct, vendor-specific* pickers on these
  flags. Each picker has a **hardcoded option list with vendor-specific launch
  args**: claude → `default`/`acceptEdits`/`plan`/`bypassPermissions`; codex →
  `--sandbox read-only --ask-for-approval on-request` etc.; cursor → its own
  mode set.
- These are **not derivable from `capabilities`** — kiro/goose/hermes/qwen all
  share `elicitation=APPROVAL_MIRROR` yet expose *no* picker; the flag marks
  "this specific vendor has a launch-time mode UI the web knows how to render,"
  which is inherently client knowledge (the option lists + args live in the web).
- **Critically:** even if the server emitted "supports a permission picker," the
  web still couldn't render one for an *unknown* community vendor — it has no
  option list or args for it. So this flag is only ever meaningful for vendors
  the web already ships bespoke UI for.

### Group E — `iconKind` (the hard blocker, must stay client-side)
- `iconKind` maps to a **bundled SVG React component** imported at build time
  (`AgentCard.tsx`: `if (iconKind === "claude") return ClaudeIcon` over 10
  imported glyphs; `SubagentsPanel.tsx` duplicates it).
- A community plugin's icon **cannot** be a server string — the web bundle has
  no `FooIcon` to import for an unknown plugin. Unknown `iconKind` already falls
  through to a generic bot glyph (qwen/hermes do this today, on purpose).
- **This field is fundamentally client-side** and can't move to the registry in
  any meaningful way. The most the server could say is "here's a URL to an SVG,"
  which is a much bigger lift (remote asset loading, sanitization) and out of
  scope.

## The insight

Only **A + B** (identity + fork_history) are genuinely server-portable, and 2.2
**already ships them**. **C, D, E are irreducibly client-side presentation** —
not because we haven't done the work, but because they *are* client concerns:
a bundled icon, a human-chosen sort order, a marketing name, and bespoke per-
vendor launch-arg UIs the web alone knows how to render.

So the real design question is narrower than "expand `NativeCodingAgent` or
not." It's: **how should the web hold the C/D/E presentation bits for the
built-in vendors, once the identity/behavior data comes from the server?**

## Options

### Option 1 — Server = data, web = presentation overlay (RECOMMENDED)
- **2.3a:** Drive Groups A + B off `/v1/harnesses`. `nativeCodingAgents.ts`
  stops being the source of identity/fork data; it fetches native rows from the
  endpoint (via the existing `useAvailableAgents`/harness-catalog query layer).
- Keep a **small client-side presentation map keyed by `key`** for C/D/E:
  `{ claude: { iconKind, sortRank, displayName, pickerCaps }, … }`. A row whose
  `key` isn't in the map renders with sensible defaults (generic bot icon,
  `sortRank=∞` → sorts last, `display_name` from the server, no picker). This is
  **exactly today's fallback behavior** for qwen/hermes icons.
- **Community harness result:** appears in the picker with correct name/behavior
  and a generic icon — the honest, working outcome. A vendor that wants brand
  polish upstreams an icon + presentation entry (a tiny, reviewable web PR).
- **Pros:** no `NativeCodingAgent` wire-shape churn; server owns identity/behavior;
  the web keeps only what is truly client knowledge; matches the 1.8 principle of
  not putting presentation on the identity dataclass. Smallest, safest diff.
- **Cons:** two sources (server data + web presentation map) — but they're
  cleanly split by *concern* (behavior vs. bundled presentation), not duplicated.

### Option 2 — Expand `NativeCodingAgent` with presentation fields
- Add `icon_kind`, `sort_rank`, `ui_display_name`, `picker_capabilities` to the
  frozen dataclass; 2.2 emits them; web reads everything from the server.
- **Pros:** one source of truth; a community harness *could* declare `sort_rank`
  and a picker-cap.
- **Cons:** (a) `iconKind` **still** can't work for community plugins (no bundled
  asset) — so the headline benefit is illusory for the field that matters most;
  (b) puts marketing/presentation on the identity wire shape — the exact
  scope-creep deferred in 1.8, and it bloats every native row for all API
  consumers; (c) `picker_capabilities` is a lie unless the web also ships the
  vendor's option list + args, so a community value renders nothing. High churn,
  low real payoff.

### Option 3 — Hybrid: server owns `sort_rank` only; web keeps icon/displayName/caps
- Add just `sort_rank` to the dataclass (it's arguably legit ordering metadata a
  contributor might want), leave icon/displayName/picker-caps client-side.
- **Pros:** lets a community harness place itself in the picker order.
- **Cons:** marginal — sort order is the least important of the three, and mixing
  "some presentation on the server, some on the web" is muddier than Option 1's
  clean behavior/presentation split. Not worth the dataclass change.

## Recommendation

**Option 1.** The research shows the web literal conflates *behavior/identity*
(portable, already on the endpoint) with *bundled presentation* (icon, sort,
marketing name, bespoke pickers — irreducibly client-side). 2.3 should split
those: server drives A+B, a small `key`-keyed web presentation map holds C/D/E
with graceful defaults for unknown keys. That gives community native harnesses a
correct, working picker entry today (generic icon), lets vendors upstream brand
polish as a trivial follow-up, and avoids bloating the identity wire shape or
shipping a picker-capability flag the web can't honor for unknown vendors.

### If Option 1 is chosen, 2.3 splits cleanly
- **2.3a (data plumb, testable):** point identity + fork_history lookups at the
  endpoint; delete the `forkHarness.ts` rebuild/preamble sets and the identity
  half of `nativeCodingAgents.ts`. Unit-testable (mock the query), no visual risk.
- **2.3b (presentation, demo-gated):** reduce `nativeCodingAgents.ts` to the
  `key`-keyed presentation map (icon/sort/displayName/pickerCaps) with the
  unknown-key defaults; wire `AgentCard`/`SubagentsPanel`/`NewChatDialog`. This
  is the piece that needs the screenshot/recording demo.

## Open sub-questions for the reviewer
1. **Marketing `display_name`:** keep "Claude Code"/"Qwen Code" as web
   presentation, or standardize the picker on the server's "Claude"/"Qwen"? (If
   the latter, Group C's `displayName` disappears entirely and the web map only
   holds icon/sort/pickerCaps.)
2. **Community icon story:** ship with generic-bot fallback now (recommended), or
   is a server-provided icon URL wanted later (separate design)?
3. **`picker_capabilities`:** confirm these stay web-side keyed by `key` — a
   community native gets no launch-time mode picker until the web ships bespoke
   UI for it, which is the honest constraint.
