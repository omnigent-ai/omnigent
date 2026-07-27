# Proposal: Customizable project icons

- Status: Draft
- Author: Serena Ruan
- Related: [`designs/PROJECTS_PRD.md`](./PROJECTS_PRD.md) (first-class projects,
  the `config` blob this builds on)

## 1. Summary

Let a project owner pick a **custom icon** (and, optionally, a color) for a
project, replacing the generic folder glyph everywhere the project is shown: the
sidebar folder, the new-session hero, and the pinned-project flyout. Purely
cosmetic and owner-private — it helps users tell projects apart at a glance in a
long sidebar.

This is an **additive, config-only** change: the icon choice is one more key in
the project's opaque `config` blob (§8 of the PRD). No new table, no migration,
no backend logic — the server already persists `config` whole and never acts on
it.

## 2. Motivation

Every project renders the same `FolderIcon`, so a sidebar with several projects
is a column of identical folders distinguishable only by name. A per-project
icon (📐 for a design project, a bug glyph for a triage project, etc.) makes the
list scannable and gives projects a sense of identity — the smallest possible
step toward the "durable workspace" framing in the PRD's overview.

It's also the cheapest customization we can ship: the plumbing (`config` read →
dialog write → render) already exists for host/workspace/agent defaults, so this
reuses all of it.

## 3. Where the icon shows today

The generic folder glyph is rendered in three places, all of which this proposal
retargets to the project's chosen icon:

| Surface | File | Today |
|---|---|---|
| Sidebar project folder | `web/src/shell/Sidebar.tsx:1034-1040` | `FolderOpenIcon` (expanded) / `FolderIcon` (collapsed), `size-4` |
| New-session hero | `web/src/shell/NewChatDialog.tsx:3030-3044` | `FolderIcon size-12` on a `?project=` landing |
| Pinned-project flyout | `web/src/shell/Sidebar.tsx:3310` | `FolderIcon size-3.5` |

## 4. Design

### 4.1 Storage — one config key, no backend change

The icon lives in the existing `config` JSON, which is client-owned vocabulary
(`web/src/lib/projectsApi.ts:15-19`, `omnigent/db/db_models.py:702-708`). Add to
the `ProjectConfig` interface:

```typescript
export interface ProjectConfig {
  host_id?: string;
  workspace?: string;
  agent_id?: string;
  use_worktree?: boolean;
  /** Custom sidebar/hero icon; a curated lucide icon id (see ICON_CHOICES).
      Unset → the default folder glyph. */
  icon?: string;
  /** Optional icon tint; a curated palette token (not free-form hex).
      Unset → inherits the default muted-foreground color. */
  icon_color?: string;
}
```

That's the entire data model. `_encode_config`/`_decode_config`
(`omnigent/stores/project_store/sqlalchemy_store.py:29-65`) already round-trip
arbitrary object keys under a 64 KiB cap; an icon id + color token is ~30 bytes.
`ProjectObject` / `CreateProjectRequest` / `UpdateProjectRequest`
(`omnigent/server/schemas.py`) take `config` as an opaque `dict[str, Any]`, so
**no schema or openapi change is required.**

### 4.2 Curated icon set (not the full lucide catalog)

Rather than expose all ~5000 lucide icons (huge picker, unbounded string to
validate, inconsistent visual weight), ship a **curated shortlist** — ~24–36
icons that read well at `size-4` and cover common project themes (folder, code,
bug, rocket, book, beaker, palette, chart, etc.). Define it once:

```typescript
// web/src/shell/projectIcons.ts
export const PROJECT_ICON_CHOICES = [
  { id: "folder", Icon: FolderIcon },
  { id: "code", Icon: CodeIcon },
  { id: "bug", Icon: BugIcon },
  // …
] as const;
```

A curated map means:
- **Bounded validation** — an unknown/removed `icon` id falls back to the folder
  glyph (same defensive posture as an unsatisfiable host hint in the composer).
- **Consistent rendering** — a single `<ProjectIcon config={} className={} />`
  helper resolves `config.icon` → component (default `FolderIcon`) and applies
  `config.icon_color`, used by all three surfaces above.
- **Small, reviewable diff** — the picker is a grid of the shortlist, not a
  searchable 5000-item list.

The color palette is likewise a **curated token set** (e.g. the theme's accent
ramp), not a free-form hex picker — keeps projects visually coherent with the
app and sidesteps contrast/theming pitfalls.

### 4.3 The picker UI

Add an "Icon" `<Field>` to `ProjectSettingsDialog.tsx` (which already reads/writes
`config` — read at `:155-166`, write at `:168-192`). The field is a small popover
trigger showing the current icon; opening it reveals the grid of curated icons
plus a row of palette swatches. This mirrors the existing
`ThemeColorPicker.tsx` popover pattern (the closest prior art for a
customization popover). Following the dialog's established convention:

- **Read:** seed local state from `stored.icon` / `stored.icon_color` in the load effect.
- **Write:** only add `icon` / `icon_color` to the submitted config when non-default (an all-default dialog still clears to `{}`).

### 4.4 Rendering

Introduce `<ProjectIcon>` and replace the three raw `FolderIcon` usages:

```tsx
// Sidebar folder — keeps the open/closed distinction by falling back to
// FolderOpenIcon only when the project uses the *default* folder icon.
<ProjectIcon config={project.config} expanded={expanded} className="size-4 shrink-0" />
```

The hero (`size-12`) and flyout (`size-3.5`) pass their own `className`, so one
helper serves all sizes. When `config.icon` is unset the helper renders exactly
what's there today, so unconfigured projects are visually unchanged.

## 5. Scope / non-goals

- **Curated set only** — no arbitrary lucide search, no user-uploaded SVG/image,
  no emoji input (deferred; emoji raises rendering/normalization questions).
- **Curated colors only** — no free-form hex, to stay theme-coherent.
- **Owner-private, cosmetic** — the icon travels with the project's `config`; it
  is never exposed to a shared-session recipient (a shared session is ungrouped
  for them anyway, PRD §9), and it has no behavioral effect.
- **No label-project support** — a label-only folder has no `config` to store an
  icon in; picking an icon promotes it to a first-class row on demand, exactly as
  renaming already does (`useConversations.ts` rename path). Documented, not a
  new mechanism.

## 6. Phasing

1. **Icon only** (XS–S): `projectIcons.ts` curated set + `<ProjectIcon>` helper +
   the three render swaps + the dialog Field (icons, no color). Config-only, no
   backend work.
2. **Color tint** (XS): add the curated palette swatches to the picker and the
   `icon_color` key.

Both are independently shippable; phase 1 alone delivers the scannability win.

## 7. Open questions

1. Curated icon count and exact set — start ~24, grow by request?
2. Color: reuse the theme accent ramp, or a dedicated project palette?
3. Should the icon also appear on the session rows *inside* a project, or only on
   the project folder/hero? (Proposed: folder/hero/flyout only, to avoid noise.)
