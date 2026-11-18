# Workspace sidebar v2

The workspace sidebar today is built around developer tasks: Files, Diffs and
Pull Requests. Its tabs also work in an unusual way; for example, clicking a
file opens a new tab without the file tree. We want Omnigent to work well for
general knowledge work, not only coding, and we want the sidebar to use the
navigation people already know from browsers, so that new users find their way
around quickly.

Sidebar v2 replaces the sidebar's contents with browser-like tabs. A tab opens
on a page and keeps its own back and forward history, and pages link to each
other the way they do on the web. The pages are:

- **Files**: the workspace tree, with the file viewer and editor beside it.
- **Diffs**: a commit list and a list of changes, each opening a review of the
  diff, including rendered Markdown diffs.
- **Pull Requests**: a filterable list of pull requests and a page for each one.

The goal is for it to work on mobile from the start, with the same pages laid
out for a narrow screen.

## Turning it on

One server feature flag, `workspace_sidebar_v2`, in
`omnigent/server/feature_flags.py`, gates the whole sidebar. It is off by
default and appears in `/v1/info`. With it off, the sidebar is unchanged.

```bash
OMNIGENT_FEATURES=workspace_sidebar_v2
```

## How data reaches it

File reads and listings go through the workspace API
(`omnigent/api/workspace/v1alpha1/workspace.proto`), which the session's host
answers. Each read takes the first route that works: direct to the host from
the desktop app, then the server's workspace API, then the REST file routes.
`docs/workspace-fast-track.md` describes the routes and their flags. Git
calls for commits and changes are being added to the workspace API for the
Diffs page.

## npm dependencies

Already in `web/package.json` and used as they are: `@pierre/diffs` renders
diffs, and `shiki` highlights code.

New:

| Package | Why |
| --- | --- |
| `@codemirror/state`, `@codemirror/view` | Core of the file viewer and editor. |
| `@codemirror/commands` | Default keymap, undo history and line commands. |
| `@codemirror/language` | Folding, indentation and bracket matching. |
| `@codemirror/search` | Find and replace. |
| `@codemirror/autocomplete` | Closing brackets as you type; no completion. |
| `@pierre/trees` | The Files tree and the file tree beside a diff review, from the same library as `@pierre/diffs`. |
| `unified`, `remark-parse`, `remark-rehype`, `hast-util-to-html`, `fast-myers-diff` | Rendered Markdown diffs: parse both sides and diff the rendered blocks. |
| `hast-util-to-jsx-runtime` | Renders a Markdown diff as React elements. |
| `@types/hast` (dev) | Types for the Markdown syntax trees. |
| `gemoji` | `:shortcode:` emoji in pull request comments, as GitHub shows them. |
| `@tanstack/query-persist-client-core` | Keeps the pull request list's cache across restarts, so the list shows at once. |
| `@base-ui/react` | Only the filterable menu (`web/src/components/ui/filter-menu.tsx`); see below. |

### Base UI

The web app's primitives use Radix. The pull request list and the diff review
toolbar need a cascading "Add filter" menu where typing searches every
submenu while the arrow keys move through the matches. Radix menus have no
filter input, and their typeahead takes the same keystrokes; Base UI's `Menu`
supports a filter input that keeps focus. We ask for a green light to use
`@base-ui/react` for this one component. Base UI may be the better long-term
base for the primitives, but moving to it is a separate decision.
