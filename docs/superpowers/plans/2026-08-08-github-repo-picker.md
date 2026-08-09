# GitHub Repo Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user with GitHub connected (Settings → Credentials) pick a repo from a searchable dropdown in the New Chat dialog's sandbox "Repository" field, instead of only typing/pasting a URL.

**Architecture:** One new server-side route (`GET /v1/credentials/github/repos`) proxies GitHub's `/user/repos` API using the caller's stored, decrypted OAuth token — the token never reaches the browser. The frontend adds a typed client function and turns the existing plain-text repository input into a combobox (reusing the exact interaction pattern already built for this dialog's git-worktree branch-name field): focus fetches the list once, typing filters it client-side, selecting an item fills the URL field, and typing anything that matches nothing keeps working as free text exactly as today.

**Tech Stack:** FastAPI + httpx (backend), React + TypeScript + Vitest/Testing Library (frontend). No new dependencies.

Spec: `docs/superpowers/specs/2026-08-08-github-repo-picker-design.md`

## Global Constraints

- Only the GitHub provider is supported (matches the existing credentials feature — `provider="github"` throughout `credentials.py`).
- Repo list is capped to GitHub's single-page maximum (100 repos), sorted most-recently-pushed first (`sort=pushed`). No pagination beyond that — anything not in the list is still reachable by pasting a URL.
- The GitHub OAuth token must never be sent to the browser at any point.
- Every failure mode (not connected, decrypt failure, GitHub API error) must leave the field working as plain text — never block or error-toast the user.
- Repos returned must cover everything the `repo` OAuth scope already grants: owner, collaborator, and organization-member affiliations (`affiliation=owner,collaborator,organization_member`).

---

### Task 1: Backend — `GET /v1/credentials/github/repos`

**Files:**
- Modify: `omnigent/server/routes/credentials.py`
- Test: `tests/server/routes/test_credentials.py`

**Interfaces:**
- Consumes: `CredentialStore.get(user_id, "github") -> UserCredential | None`, `CredentialStore.decrypt_token(cred) -> str | None` (both already exist in `omnigent/stores/credential_store.py`).
- Produces: `_fetch_github_repos(token: str) -> list[dict[str, Any]]` (a monkeypatch seam for route-level tests, mirroring the existing `_fetch_github_user`). Route response shape on success: `{"repos": [{"full_name": str, "clone_url": str, "default_branch": str, "private": bool}, ...]}`. On failure: an `OmnigentError` — `code=ErrorCode.CONFLICT` (409) when not connected or the token can't be decrypted, `code=ErrorCode.INTERNAL_ERROR` (500) when the GitHub call itself fails.

- [ ] **Step 1: Write the failing tests**

Add to `tests/server/routes/test_credentials.py`, after the existing `test_callback_happy_path_then_list_and_disconnect` test (end of file):

```python
async def test_repos_requires_connected_github(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/credentials/github/repos")
    assert resp.status_code == 409


async def test_repos_returns_mapped_list(
    client: httpx.AsyncClient, credential_store: CredentialStore, monkeypatch
) -> None:
    credential_store.upsert(_USER, "github", token="gho_live", login="alice-gh", scopes="repo")

    async def fake_repos(token: str) -> list[dict]:
        assert token == "gho_live"
        return [
            {
                "full_name": "alice/proj",
                "clone_url": "https://github.com/alice/proj.git",
                "default_branch": "main",
                "private": False,
                "id": 1,  # extra GitHub fields the mapping must drop
            },
        ]

    monkeypatch.setattr(credentials_routes, "_fetch_github_repos", fake_repos)
    resp = await client.get("/v1/credentials/github/repos")
    assert resp.status_code == 200
    assert resp.json() == {
        "repos": [
            {
                "full_name": "alice/proj",
                "clone_url": "https://github.com/alice/proj.git",
                "default_branch": "main",
                "private": False,
            }
        ]
    }


async def test_repos_undecryptable_credential_treated_as_not_connected(
    client: httpx.AsyncClient, credential_store: CredentialStore, monkeypatch
) -> None:
    credential_store.upsert(_USER, "github", token="gho_live", login="alice-gh", scopes="repo")
    # Rotate the key after writing — the stored ciphertext no longer decrypts.
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    resp = await client.get("/v1/credentials/github/repos")
    assert resp.status_code == 409


async def test_repos_github_api_failure_returns_500(
    client: httpx.AsyncClient, credential_store: CredentialStore, monkeypatch
) -> None:
    credential_store.upsert(_USER, "github", token="gho_live", login="alice-gh", scopes="repo")

    async def failing_repos(token: str) -> list[dict]:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(credentials_routes, "_fetch_github_repos", failing_repos)
    resp = await client.get("/v1/credentials/github/repos")
    assert resp.status_code == 500


@respx.mock
async def test_fetch_github_repos_requests_all_affiliations_sorted_by_pushed() -> None:
    route = respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "full_name": "alice/proj",
                    "clone_url": "https://github.com/alice/proj.git",
                    "default_branch": "main",
                    "private": False,
                }
            ],
        )
    )
    repos = await credentials_routes._fetch_github_repos("gho_live")
    assert repos[0]["full_name"] == "alice/proj"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer gho_live"
    assert request.url.params["affiliation"] == "owner,collaborator,organization_member"
    assert request.url.params["sort"] == "pushed"
    assert request.url.params["per_page"] == "100"
```

Add `import respx` to the imports at the top of the file, alongside the existing `import httpx`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent && source .venv/bin/activate && python3 -m pytest tests/server/routes/test_credentials.py -v -k repos`
Expected: FAIL — `404 Not Found` on the new route (doesn't exist yet), and `AttributeError: module 'omnigent.server.routes.credentials' has no attribute '_fetch_github_repos'` for the respx test.

- [ ] **Step 3: Implement `_fetch_github_repos`**

In `omnigent/server/routes/credentials.py`, add this function immediately after `_fetch_github_user` (which ends right before `def create_credentials_router`):

```python
async def _fetch_github_repos(token: str) -> list[dict[str, Any]]:
    """Fetch repos the token can access — owner, collaborator, and org member
    affiliations, most-recently-pushed first, capped at GitHub's 100/page
    maximum (raises on HTTP error)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_USER_URL}/repos",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            params={
                "affiliation": "owner,collaborator,organization_member",
                "sort": "pushed",
                "per_page": 100,
            },
        )
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Implement the route**

In `omnigent/server/routes/credentials.py`, add this route inside `create_credentials_router`, immediately after `list_credentials` and before `connect_github`:

```python
    @router.get("/v1/credentials/github/repos")
    async def list_github_repos(request: Request) -> dict[str, Any]:
        """The caller's GitHub repos — owner, collaborator, and org member."""
        user_id = _user(request)
        cred = credential_store.get(user_id, "github")
        if cred is None:
            raise OmnigentError("github_not_connected", code=ErrorCode.CONFLICT)
        token = credential_store.decrypt_token(cred)
        if token is None:
            raise OmnigentError("github_not_connected", code=ErrorCode.CONFLICT)
        try:
            repos = await _fetch_github_repos(token)
        except httpx.HTTPError as exc:
            logger.warning("github repo listing failed for %s", user_id, exc_info=True)
            raise OmnigentError(
                "github_repos_fetch_failed", code=ErrorCode.INTERNAL_ERROR
            ) from exc
        return {
            "repos": [
                {
                    "full_name": r["full_name"],
                    "clone_url": r["clone_url"],
                    "default_branch": r["default_branch"],
                    "private": r["private"],
                }
                for r in repos
            ]
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent && source .venv/bin/activate && python3 -m pytest tests/server/routes/test_credentials.py -v`
Expected: PASS — all tests in the file, including the 5 new ones.

- [ ] **Step 6: Lint and type-check**

Run: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent && source .venv/bin/activate && ruff format omnigent/server/routes/credentials.py tests/server/routes/test_credentials.py && ruff check omnigent/server/routes/credentials.py tests/server/routes/test_credentials.py`
Expected: no changes needed beyond formatting, no lint errors.

- [ ] **Step 7: Commit**

```bash
cd /Users/pzharyuk/Documents/Claude/Projects/omnigent
git add omnigent/server/routes/credentials.py tests/server/routes/test_credentials.py
git commit -m "feat(credentials): add GET /v1/credentials/github/repos

Server-side proxy that lists the caller's GitHub repos (owner,
collaborator, and org-member affiliations) using their stored,
decrypted OAuth token. The token never reaches the browser."
```

---

### Task 2: Frontend — repo picker combobox in the New Chat dialog

**Files:**
- Modify: `web/src/lib/credentialsApi.ts`
- Modify: `web/src/shell/NewChatDialog.tsx`
- Test: `web/src/shell/NewChatDialog.test.tsx`

**Interfaces:**
- Consumes: `GET /v1/credentials/github/repos` from Task 1 (response shape `{"repos": [{"full_name", "clone_url", "default_branch", "private"}]}` on 200; non-2xx on failure).
- Produces: `listGithubRepos(): Promise<GithubReposResult>` where `GithubReposResult = { ok: true; repos: GithubRepoInfo[] } | CredentialsFailure`, `GithubRepoInfo = { full_name: string; clone_url: string; default_branch: string; private: boolean }`. Both exported from `web/src/lib/credentialsApi.ts`.

- [ ] **Step 1: Write the failing frontend tests**

In `web/src/shell/NewChatDialog.test.tsx`, add this mock setup near the top of the file, alongside the other `vi.mock` calls (after the `vi.mock("@/hooks/useHosts", ...)` block):

```typescript
const credentialsMocks = vi.hoisted(() => ({
  listGithubRepos: vi.fn(),
}));
vi.mock("@/lib/credentialsApi", () => credentialsMocks);
```

In `setupLandingMocks` (the function used as the `NewChatLandingScreen` describe block's `beforeEach`), add a reset and a safe default so every other existing test — which doesn't care about this feature — sees the field behave exactly as before:

```typescript
  credentialsMocks.listGithubRepos.mockReset();
  credentialsMocks.listGithubRepos.mockResolvedValue({
    ok: false,
    error: "github_not_connected",
    status: 409,
  });
```
(Add these two lines right after the existing `authenticatedFetchMock.mockReset();` line in `setupLandingMocks`.)

Then add these three tests inside the `describe("NewChatLandingScreen", ...)` block, after the existing `it("blocks submit on an invalid repository URL or a dangling branch", ...)` test:

```typescript
  it("shows connected GitHub repos in a filterable dropdown and fills the URL on selection", async () => {
    credentialsMocks.listGithubRepos.mockResolvedValue({
      ok: true,
      repos: [
        {
          full_name: "alice/proj-one",
          clone_url: "https://github.com/alice/proj-one.git",
          default_branch: "main",
          private: false,
        },
        {
          full_name: "alice/proj-two",
          clone_url: "https://github.com/alice/proj-two.git",
          default_branch: "main",
          private: true,
        },
      ],
    });
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    fireEvent.focus(screen.getByTestId("new-chat-landing-repo-input"));
    await waitFor(() => expect(credentialsMocks.listGithubRepos).toHaveBeenCalledTimes(1));
    const options = await screen.findAllByTestId("new-chat-landing-repo-option");
    expect(options).toHaveLength(2);

    fireEvent.change(screen.getByTestId("new-chat-landing-repo-input"), {
      target: { value: "proj-two" },
    });
    await waitFor(() =>
      expect(screen.getAllByTestId("new-chat-landing-repo-option")).toHaveLength(1),
    );

    fireEvent.mouseDown(screen.getByTestId("new-chat-landing-repo-option"));
    expect((screen.getByTestId("new-chat-landing-repo-input") as HTMLInputElement).value).toBe(
      "https://github.com/alice/proj-two.git",
    );
  });

  it("shows a connect-GitHub hint when GitHub isn't connected", async () => {
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    fireEvent.focus(screen.getByTestId("new-chat-landing-repo-input"));
    await waitFor(() => expect(credentialsMocks.listGithubRepos).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("Connect GitHub in Settings to browse your repos"),
    ).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-repo-option")).toBeNull();
  });

  it("still accepts an arbitrary pasted URL when the repo list is loaded", async () => {
    credentialsMocks.listGithubRepos.mockResolvedValue({
      ok: true,
      repos: [
        {
          full_name: "alice/proj-one",
          clone_url: "https://github.com/alice/proj-one.git",
          default_branch: "main",
          private: false,
        },
      ],
    });
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    fireEvent.focus(screen.getByTestId("new-chat-landing-repo-input"));
    await waitFor(() => expect(credentialsMocks.listGithubRepos).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByTestId("new-chat-landing-repo-input"), {
      target: { value: "https://github.com/someone-else/other-repo" },
    });
    expect(screen.queryByTestId("new-chat-landing-repo-option")).toBeNull();
    expect((screen.getByTestId("new-chat-landing-repo-input") as HTMLInputElement).value).toBe(
      "https://github.com/someone-else/other-repo",
    );
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent/web && npx vitest run src/shell/NewChatDialog.test.tsx -t "GitHub repo"`
Expected: FAIL — `listGithubRepos` doesn't exist in `@/lib/credentialsApi`, and `new-chat-landing-repo-option`/the connect hint don't exist in the DOM yet.

- [ ] **Step 3: Add `listGithubRepos` to `credentialsApi.ts`**

Append to the end of `web/src/lib/credentialsApi.ts`:

```typescript
/** One repo the caller's connected GitHub credential can access. */
export interface GithubRepoInfo {
  full_name: string;
  clone_url: string;
  default_branch: string;
  private: boolean;
}

export type GithubReposResult = { ok: true; repos: GithubRepoInfo[] } | CredentialsFailure;

/** GET /v1/credentials/github/repos — the caller's accessible GitHub repos. */
export async function listGithubRepos(): Promise<GithubReposResult> {
  let res: Response;
  try {
    res = await authenticatedFetch("/v1/credentials/github/repos");
  } catch {
    return NETWORK_FAILURE;
  }
  if (res.ok) {
    const data = (await res.json()) as { repos: GithubRepoInfo[] };
    return { ok: true, repos: data.repos };
  }
  return failureFrom(res, "Could not load your GitHub repos.");
}
```

Note: `credentialsApi.ts` already has `import { authenticatedFetch } from "@/lib/identity";` at
the top (its other three functions were migrated from bare `fetch()` to
`authenticatedFetch` in a prerequisite fix — the project's `no-restricted-globals`
oxlint rule bans bare `fetch()` outside `src/lib/host.ts`). Use the existing
import; do not add a second one.

- [ ] **Step 4: Add state and the filtered-repos memo to `NewChatDialog.tsx`**

Add the import, alongside the other `@/lib/*` imports (after `import { cn } from "@/lib/utils";`):

```typescript
import { listGithubRepos, type GithubRepoInfo } from "@/lib/credentialsApi";
```

Find this block (the `sandboxRepoBranch` state declaration):

```typescript
  const [sandboxRepoBranch, setSandboxRepoBranch] = useState<string>(
    () => landingDraft?.sandboxRepoBranch ?? "",
  );
```

Immediately after it, add:

```typescript
  // The repository field doubles as a combobox: focusing it fetches the
  // caller's GitHub repos (once per dialog instance) if connected, and
  // typing filters them. A value matching none is used as a plain URL,
  // unchanged from the field's pre-picker behavior.
  type GithubRepoPickerState =
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ready"; repos: GithubRepoInfo[] }
    | { status: "not_connected" }
    | { status: "error" };
  const [githubRepoPicker, setGithubRepoPicker] = useState<GithubRepoPickerState>({
    status: "idle",
  });
  const [repoInputFocused, setRepoInputFocused] = useState(false);
  const fetchGithubRepos = useCallback(() => {
    if (githubRepoPicker.status !== "idle") return;
    setGithubRepoPicker({ status: "loading" });
    void listGithubRepos().then((result) => {
      if (!result.ok) {
        setGithubRepoPicker(
          result.status === 409 ? { status: "not_connected" } : { status: "error" },
        );
        return;
      }
      setGithubRepoPicker({ status: "ready", repos: result.repos });
    });
  }, [githubRepoPicker]);
  const filteredGithubRepos = useMemo(() => {
    if (githubRepoPicker.status !== "ready") return [];
    const q = sandboxRepoUrl.trim().toLowerCase();
    if (q === "") return githubRepoPicker.repos;
    return githubRepoPicker.repos.filter((r) => r.full_name.toLowerCase().includes(q));
  }, [githubRepoPicker, sandboxRepoUrl]);
```

- [ ] **Step 5: Wire the combobox into the repository popover JSX**

Find this block (the sandbox repository chip's URL input, currently a plain `<input>` directly inside the `flex flex-col gap-2` popover content):

```typescript
                      <input
                        id="landing-repo-url"
                        type="text"
                        value={sandboxRepoUrl}
                        onChange={(e) => setSandboxRepoUrl(e.target.value)}
                        placeholder="https://github.com/org/repo"
                        className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                        data-testid="new-chat-landing-repo-input"
                      />
```

Replace it with:

```typescript
                      <div className="relative flex flex-col">
                        <input
                          id="landing-repo-url"
                          type="text"
                          value={sandboxRepoUrl}
                          onChange={(e) => setSandboxRepoUrl(e.target.value)}
                          onFocus={() => {
                            setRepoInputFocused(true);
                            fetchGithubRepos();
                          }}
                          // Delay so a click on a dropdown option registers
                          // before the list unmounts on blur.
                          onBlur={() => setTimeout(() => setRepoInputFocused(false), 120)}
                          placeholder="https://github.com/org/repo"
                          role="combobox"
                          aria-expanded={repoInputFocused && filteredGithubRepos.length > 0}
                          aria-autocomplete="list"
                          className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                          data-testid="new-chat-landing-repo-input"
                        />
                        {repoInputFocused && filteredGithubRepos.length > 0 && (
                          <div
                            className="absolute top-full right-0 left-0 z-20 mt-1 flex max-h-40 flex-col overflow-y-auto rounded-md border border-input bg-popover p-1 shadow-md"
                            data-testid="new-chat-landing-repo-dropdown"
                          >
                            <ul className="flex flex-col gap-0.5">
                              {filteredGithubRepos.map((r) => (
                                <li key={r.full_name}>
                                  <button
                                    type="button"
                                    // onMouseDown (not onClick): fires before
                                    // the input's blur, so the selection lands
                                    // even though blur is about to hide the list.
                                    onMouseDown={(e) => {
                                      e.preventDefault();
                                      setSandboxRepoUrl(r.clone_url);
                                      setRepoInputFocused(false);
                                    }}
                                    className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1 text-left text-xs transition-colors hover:bg-accent"
                                    data-testid="new-chat-landing-repo-option"
                                  >
                                    <span className="truncate text-foreground">
                                      {r.full_name}
                                    </span>
                                    {r.private && (
                                      <span className="shrink-0 text-[10px] text-muted-foreground">
                                        private
                                      </span>
                                    )}
                                  </button>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>
                      {githubRepoPicker.status === "not_connected" && (
                        <p className="text-xs text-muted-foreground">
                          Connect GitHub in Settings to browse your repos
                        </p>
                      )}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent/web && npx vitest run src/shell/NewChatDialog.test.tsx`
Expected: PASS — the full file, including the 3 new tests and every pre-existing test in it (the repo-picker changes must not regress `"sends the repository inputs as the managed workspace string"`, `"shows host-provided git credentials tooltip content..."`, or `"blocks submit on an invalid repository URL..."`).

- [ ] **Step 7: Type-check and lint**

Run: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent/web && npx tsc --noEmit && node_modules/.bin/oxlint --deny-warnings src/lib/credentialsApi.ts src/shell/NewChatDialog.tsx src/shell/NewChatDialog.test.tsx && node_modules/.bin/prettier --check src/lib/credentialsApi.ts src/shell/NewChatDialog.tsx src/shell/NewChatDialog.test.tsx`
Expected: no type errors, no lint errors, no formatting diffs.

- [ ] **Step 8: Commit**

```bash
cd /Users/pzharyuk/Documents/Claude/Projects/omnigent
git add web/src/lib/credentialsApi.ts web/src/shell/NewChatDialog.tsx web/src/shell/NewChatDialog.test.tsx
git commit -m "feat(web): GitHub repo picker in the sandbox Repository field

Focusing the repository field in the New Chat dialog's sandbox flow
now fetches the caller's connected GitHub repos (once per dialog
instance) and offers them as a filterable dropdown, alongside the
existing free-text URL entry. Falls back silently to plain text when
GitHub isn't connected or the fetch fails."
```

---

## Post-implementation checklist

- [ ] Run the full backend suite touching this area: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent && source .venv/bin/activate && python3 -m pytest tests/server/routes/test_credentials.py -v`
- [ ] Run the full frontend suite for the touched file: `cd /Users/pzharyuk/Documents/Claude/Projects/omnigent/web && npx vitest run src/shell/NewChatDialog.test.tsx`
- [ ] `pre-commit run --files omnigent/server/routes/credentials.py tests/server/routes/test_credentials.py web/src/lib/credentialsApi.ts web/src/shell/NewChatDialog.tsx web/src/shell/NewChatDialog.test.tsx`
- [ ] Manually verify in a running server with `OMNIGENT_GITHUB_CREDENTIAL_CLIENT_ID`/`_SECRET` configured and GitHub connected: opening the sandbox Repository popover and focusing the URL field shows your repos; typing filters them; selecting one fills the field; pasting an unrelated URL still works.
