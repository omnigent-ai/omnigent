# GitLab merge requests

GitLab sessions show their merge requests in the shared pull request panel,
composer controls, and Canvas cards. The execution host's authenticated `glab`
reads the MR; agent CLI and MCP writes associate it with the session.

## Sub-features

- `summary`: MR identity, nested project, description, comments, and checks.
- `changes`: changed files and diff, with exact-revision expanded context.
- `associations`: link, select, and unlink MRs across projects.
- `partial`: incomplete comments, checks, files, and omitted diff explanations.
- `auth`: missing CLI or denied access retains checkout context and a useful hint.
- `discovery`: glab-configured hosts are recognized without Omnigent configuration,
  including alongside GitHub and Azure DevOps remotes.
- `tracking`: successful mutations persist; reads, comments, failures, and replay
  do not create extra associations.

## How to get to it (user POV)

- `desktop-rail`: open a session's Workspace sidepanel and choose Pull Requests.
- `desktop-composer`: click the MR number beside the session composer.
- `mobile-composer`: tap the MR number to open its full-screen panel.
- `canvas`: enable Canvas, open it, and follow an MR link on a session card.
- `native-hook`: create or update an MR through a Claude or Codex native session.

## Driving it with the repro environment

Preconditions: use the [Verify Omnigent skill](skills/verify-omnigent/SKILL.md)
and start an isolated instance. Browser fixtures replace GitLab resource
responses; they exercise the actual shared UI without contacting GitLab.

- `desktop-rail`, `desktop-composer`, and `mobile-composer`:
  `tests/e2e_ui/gitlab/test_gitlab_panel.py::test_gitlab_panel_summary_and_diff`
  shows `!7`, comments and checks, then opens the file diff and expands unchanged
  context with the pinned MR revisions. Record every variant.
- Associations:
  `tests/e2e_ui/gitlab/test_gitlab_panel.py::test_gitlab_link_select_unlink`
  attaches a second project, changes selection, and removes it.
- Partial responses:
  `tests/e2e_ui/gitlab/test_gitlab_panel.py::test_gitlab_partial_data_is_visible`
  checks visible summary and diff limitations.
- `canvas`:
  `tests/e2e_ui/gitlab/test_gitlab_panel.py::test_gitlab_canvas_link_uses_merge_request_identity`
  checks the MR number and the exact external destination.
- `native-hook` (own environment):
  `tests/e2e/test_gitlab_pr_tracking_e2e.py::test_native_hook_tracks_gitlab_mr_without_observer_io`
  runs Claude/Codex hook subprocesses through a local relay, persists the MR,
  and reads it through session resources. The CLI is a deterministic fixture;
  its call log proves the observer made no CLI request.
- Live auth, fork context, and removal persistence: follow
  [the manual verification steps](../docs/GITLAB.md#limits-and-verification)
  using a permitted test GitLab project and execution-host credentials.

## Gotchas

- The shared panel tab and link actions retain the generic Pull Requests wording.
  The provider heading, MR number prefix, external links, and Canvas label must
  use GitLab metadata.
- A private instance is discovered from the execution host's `glab auth login`
  config. Nondefault HTTPS ports stay part of that saved host. SSH ports are separate;
  `api_host` routing remains owned by glab.
- Mocked browser responses prove rendering and interaction, not GitLab access.
  The native hook fixture proves transport and persistence, not vendor services.
- Keep evidence outside the tracked tree. Canvas is gated by its feature flag;
  desktop sidepanel proof does not cover the mobile drawer or Canvas link.
