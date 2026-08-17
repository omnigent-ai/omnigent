# GitHub repo picker for the sandbox "Repository" field

## Problem

The New Chat dialog's sandbox flow asks for a git repository URL as a plain
text field (`Repository (optional)` in the sandbox repository chip,
`web/src/shell/NewChatDialog.tsx`). A user who has connected GitHub via
Settings → Credentials (the per-user credentials feature) already has an
OAuth token with `repo` scope on file server-side, but has no way to browse
their own repos from this dialog — they still have to copy/paste a URL from
GitHub.

## Goals

- Let a user with GitHub connected pick a repo from a searchable list instead
  of typing/pasting a URL.
- Never regress the existing behavior: pasting an arbitrary URL must keep
  working exactly as it does today, for repos not in the list (private org
  repos outside the returned page, repos under an account other than the
  connected one, non-GitHub git hosts) and for users who haven't connected
  GitHub at all.
- Never expose the stored GitHub token to the browser.

## Non-goals

- Providers other than GitHub (the credentials feature only supports GitHub
  today — `provider="github"` is effectively hardcoded through
  `credentials.py`).
- Repo lists beyond GitHub's single-page maximum (100 repos). Anything
  outside that is reachable via manual URL entry, same as today.
- Persisting or syncing the repo list server-side; it's fetched live on
  demand.

## Architecture

### Backend: `GET /v1/credentials/github/repos`

Added to `omnigent/server/routes/credentials.py`, alongside the existing
`GET /v1/credentials`, `POST /v1/credentials/github/connect`, and
`DELETE /v1/credentials/github` routes, following the same conventions:

- Resolves the caller via the existing `_user(request)` helper.
- Looks up the credential via `credential_store.get(user_id, "github")`; if
  `None`, returns a `not_connected` failure (see Error handling below).
- Decrypts the token via `credential_store.decrypt_token(cred)` (same call
  used by `_owner_credential_env` in
  `omnigent/server/routes/_sessions/helpers.py`); if decryption fails
  (rotated encryption key), treat identically to "not connected."
- Calls `GET https://api.github.com/user/repos` with
  `affiliation=owner,collaborator,organization_member&sort=pushed&per_page=100`
  using the same `httpx.AsyncClient(timeout=15.0)` pattern as
  `_fetch_github_user`. This surfaces owned, collaborator, and
  organization-member repos — everything the `repo` scope already grants
  access to — sorted most-recently-pushed first, capped at GitHub's 100/page
  maximum.
- Maps the response to a compact shape per repo: `full_name`, `clone_url`,
  `default_branch`, `private`. The rest of GitHub's per-repo payload is
  discarded — the client never needs it.
- On any `httpx.HTTPError` from the GitHub call, returns a generic failure
  (see Error handling).

### Frontend: `credentialsApi.ts`

New `listGithubRepos()` function, following the existing discriminated-union
result pattern in this file:

```ts
export interface GithubRepoInfo {
  fullName: string;
  cloneUrl: string;
  defaultBranch: string;
  private: boolean;
}

export type GithubReposResult =
  | { ok: true; repos: GithubRepoInfo[] }
  | CredentialsFailure;

export async function listGithubRepos(): Promise<GithubReposResult> { … }
```

### Frontend: `NewChatDialog.tsx`

The plain `<input id="landing-repo-url">` inside the sandbox repository
popover becomes a combobox, reusing the exact interaction pattern already
implemented a few hundred lines down for the git-worktree branch-name field
(`landing-branch-name`: `onFocus`-revealed dropdown, blur-with-delay close,
type-to-filter, `filteredWorktrees`-style local filtering):

- On first `onFocus` of the repository field, if not already fetched this
  dialog instance, call `listGithubRepos()` once and store the result in
  component state (`githubRepos`, `githubReposFetched`).
- While the field is focused and `githubRepos` is non-empty, render a
  dropdown of repos filtered by case-insensitive substring match against
  `full_name`, in a scrollable container capped to a fixed max-height (same
  `max-h-*` + `overflow-y-auto` treatment the worktree dropdown already uses)
  so a 100-repo list doesn't blow out the popover — no separate row-count
  cap, the scroll container handles it.
- Clicking a repo sets `sandboxRepoUrl` to its `clone_url` and closes the
  dropdown. The `default_branch` is available on the object but is **not**
  auto-filled into the branch field — that field's placeholder already says
  "defaults to the repo's default," so leaving it blank is correct and
  auto-filling would be redundant.
- Typing text that matches no repo leaves the typed value as-is in
  `sandboxRepoUrl` — unchanged from today's plain-text behavior.
- Fetched-but-not-connected or fetch-failed states render no dropdown items;
  a small `text-muted-foreground` hint appears below the field: "Connect
  GitHub in Settings to browse your repos" (only shown in the
  not-connected case, not on a transient fetch error — an error is silent,
  matching this popover's low-stakes, non-blocking tone elsewhere).
- State resets when the dialog unmounts/remounts (no cross-session cache).

## Data flow

1. User opens the sandbox "Repository" popover and focuses the URL field.
2. First focus triggers one `GET /v1/credentials/github/repos` call.
3. Connected + success → server decrypts the token, calls GitHub, returns
   the mapped list → frontend renders it as a filterable dropdown.
4. Typing filters the list client-side; selecting an item fills the URL
   field and closes the dropdown.
5. Not connected, decrypt failure, or GitHub API failure → dropdown stays
   empty (with the inline hint only for the not-connected case); the field
   works exactly as a plain text input, same as before this feature existed.

## Error handling

| Condition | Backend response | Frontend behavior |
|---|---|---|
| No GitHub credential on file | `{ok:false, error:"not_connected", status:409}`-shaped failure | Inline hint, no console noise |
| Token decrypt fails (key rotated) | Same as not connected | Same as not connected |
| GitHub API error/timeout/rate limit | Generic failure, logged server-side (matches existing `logger.warning` pattern in this file) | Silent fallback to plain text, `console.warn` only |

None of these block the user from typing/pasting a URL and proceeding — the
repo picker is purely additive.

## Testing

**Backend** (`tests/server/routes/test_credentials.py`, extending the
existing structure):
- Connected + GitHub returns repos → 200 with the mapped list.
- Not connected → `not_connected` failure.
- Token decrypt failure → treated as not connected.
- GitHub API failure (mocked `httpx` error) → generic failure, no
  unhandled exception.

**Frontend**:
- Extend `credentialsApi` tests with `listGithubRepos()` success/failure
  cases.
- `NewChatDialog` test: repos populate the dropdown once connected,
  filtering narrows the list as you type, clicking an item fills
  `sandboxRepoUrl`, and typing an arbitrary URL (no matching repo) still
  sets the field directly — the regression case that matters most.
