import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import {
  composeSandboxWorkspace,
  deriveHomeDir,
  deriveRepoName,
  describeCreateError,
  harnessUnconfiguredOnHost,
  isValidSandboxRepoUrl,
  isValidWorkspace,
  matchSkillInvocation,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
  NewChatLandingScreen,
} from "./NewChatDialog";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import type { Conversation } from "@/hooks/useConversations";
import { setOmnigentHostConfig } from "@/lib/host";
import { TooltipProvider } from "@/components/ui/tooltip";

// Only authenticatedFetch is stubbed (the create POST under test);
// the module's other exports stay real for any other consumer in the tree.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/identity")>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: vi.fn(),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: vi.fn(),
}));

const authenticatedFetchMock = vi.mocked(authenticatedFetch);
const useHostsMock = vi.mocked(useHosts);
const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const useDirectorySessionsMock = vi.mocked(useDirectorySessions);
const useRunnerHealthMock = vi.mocked(useRunnerHealthRegistration);

const RECENT_KEY = "omnigent:recent-workspaces";

/**
 * Build a minimal Conversation for the directory-conflict helpers/warning.
 * Defaults to a *live* session (bound runner, idle) so it counts toward
 * the conflict tally; `host_id` + `workspace` drive the match. Override
 * `runner_id`/`status` to model an inactive session.
 */
function conv(overrides: Partial<Conversation>): Conversation {
  return {
    id: "conv_x",
    object: "conversation",
    title: null,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    runner_id: "runner_1",
    status: "idle",
    ...overrides,
  };
}

/** A real HostFilesystemEntry for the home-derivation tests. */
function fsEntry(path: string): HostFilesystemEntry {
  return {
    name: path.split("/").filter(Boolean).pop() ?? "",
    path,
    type: "directory",
    bytes: null,
    modified_at: 0,
  };
}

// describeCreateError only reads .status and .json(); a minimal stub
// keeps the test independent of the global Response implementation.
function fakeResponse(status: number, json: () => Promise<unknown>): Response {
  return { status, json } as unknown as Response;
}

// Workspace validation contract — pins the same shape the server
// validator enforces (per designs/SESSION_WORKSPACE_SELECTION.md):
// tilde-prefixed and relative paths are rejected; only
// fully-absolute paths starting with `/` are accepted. If this
// drifts out of sync with the server, the submit button would
// either let through requests the server rejects (opaque 400) or
// block requests the server would accept (button stuck disabled).
describe("isValidWorkspace", () => {
  it("accepts a fully absolute path", () => {
    expect(isValidWorkspace("/Users/corey/projects/myapp")).toBe(true);
  });

  it("accepts root path", () => {
    // The root `/` is a valid absolute path. Edge case worth pinning
    // because trimming logic could mis-classify a single-char input.
    expect(isValidWorkspace("/")).toBe(true);
  });

  it("trims whitespace before checking", () => {
    // Browsers paste with stray whitespace; trim must run before
    // the shape check or "  /Users/corey  " would be rejected.
    expect(isValidWorkspace("  /Users/corey  ")).toBe(true);
  });

  it("rejects empty string", () => {
    // Disabled-by-default state. Without this rejection, the submit
    // button would enable as soon as the user clicks the input.
    expect(isValidWorkspace("")).toBe(false);
  });

  it("rejects whitespace-only input", () => {
    expect(isValidWorkspace("   ")).toBe(false);
  });

  it("rejects tilde-prefixed paths", () => {
    // The server explicitly rejects ~ in the workspace request body
    // (only the host expands ~). If the UI silently accepted this,
    // every "~/..." submit would surface a confusing 400 from the
    // server instead of an inline disabled-button hint.
    expect(isValidWorkspace("~/projects")).toBe(false);
    expect(isValidWorkspace("~")).toBe(false);
  });

  it("rejects relative paths", () => {
    expect(isValidWorkspace("projects/myapp")).toBe(false);
    expect(isValidWorkspace("./myapp")).toBe(false);
    expect(isValidWorkspace("../myapp")).toBe(false);
  });
});

// Path normalization underpins the directory-conflict match: a freshly
// typed path and a stored workspace must compare equal despite trailing-
// slash / whitespace differences, or the warning would silently miss
// (false-equal) or false-warn.
describe("normalizeWorkspacePath", () => {
  it.each<[string, string | null]>([
    ["/Users/me/repo", "/Users/me/repo"],
    // Trailing slash dropped so "/repo/" matches a stored "/repo".
    ["/Users/me/repo/", "/Users/me/repo"],
    ["/Users/me/repo///", "/Users/me/repo"],
    // Surrounding whitespace (pasted paths) trimmed before comparison.
    ["  /a/b  ", "/a/b"],
    // Root is preserved, not collapsed away.
    ["/", "/"],
    ["///", "/"],
    // Blank → null (no path) — must NOT become "/", or an empty input would
    // spuriously match a session whose workspace is the root.
    ["", null],
    ["   ", null],
  ])("normalizes %j to %j", (input, expected) => {
    expect(normalizeWorkspacePath(input)).toBe(expected);
  });
});

// The warning's count comes from this filter. The table pins both the positive
// match (incl. trailing-slash normalization on either side) and every reason
// a session must NOT count — wrong host, wrong dir, null workspace, offline
// runner — so the warning can't fire on unrelated/dead sessions. `offline`
// lists ids whose runner is down; the rest are treated as online.
describe("sessionsSharingDirectory", () => {
  // Online sessions sharing /repo on host_1 = a + b; the rest are decoys,
  // each covering one non-match reason.
  const base: Conversation[] = [
    conv({ id: "a", host_id: "host_1", workspace: "/repo" }),
    conv({ id: "b", host_id: "host_1", workspace: "/repo/" }),
    conv({ id: "c", host_id: "host_2", workspace: "/repo" }), // wrong host
    conv({ id: "d", host_id: "host_1", workspace: "/other" }), // wrong dir
    conv({ id: "e", host_id: "host_1", workspace: null }), // no workspace
  ];

  const cases: {
    name: string;
    sessions: Conversation[];
    hostId: string | null;
    workspace: string;
    offline: string[];
    expected: string[];
  }[] = [
    {
      name: "matches host + dir, normalizing a stored trailing slash",
      sessions: base,
      hostId: "host_1",
      workspace: "/repo",
      offline: [],
      expected: ["a", "b"],
    },
    {
      name: "normalizes a trailing slash on the typed path too",
      sessions: base,
      hostId: "host_1",
      workspace: "/repo/",
      offline: [],
      expected: ["a", "b"],
    },
    {
      name: "returns [] when no host is selected",
      sessions: base,
      hostId: null,
      workspace: "/repo",
      offline: [],
      expected: [],
    },
    {
      name: "returns [] for a blank workspace",
      sessions: base,
      hostId: "host_1",
      workspace: "  ",
      offline: [],
      expected: [],
    },
    {
      name: "returns [] when nothing shares the directory",
      sessions: base,
      hostId: "host_1",
      workspace: "/nowhere",
      offline: [],
      expected: [],
    },
    {
      // Offline runner ⇒ no live process ⇒ no conflict; same connectivity
      // gate as the sidebar's dots. x shares the dir but is down.
      name: "excludes sessions whose runner is offline",
      sessions: [
        conv({ id: "a", host_id: "host_1", workspace: "/repo" }),
        conv({ id: "x", host_id: "host_1", workspace: "/repo" }),
      ],
      hostId: "host_1",
      workspace: "/repo",
      offline: ["x"],
      expected: ["a"],
    },
    {
      // openui excludes only *disconnected* agents, not errored ones — a
      // failed session whose runner is still online occupies the dir. Guards
      // against re-adding a status-based filter.
      name: "counts a failed session whose runner is still online",
      sessions: [conv({ id: "f", host_id: "host_1", workspace: "/repo", status: "failed" })],
      hostId: "host_1",
      workspace: "/repo",
      offline: [],
      expected: ["f"],
    },
  ];

  it.each(cases)("$name", ({ sessions, hostId, workspace, offline, expected }) => {
    const isOnline = (id: string) => !offline.includes(id);
    expect(
      sessionsSharingDirectory(sessions, hostId, workspace, isOnline).map((s) => s.id),
    ).toEqual(expected);
  });
});

// The sandbox repository inputs mirror the server's parse_repo_workspace
// grammar: these pin the client-side gate (URL shapes), the reassembly into
// the one-string `<url>[#<branch>]` workspace, and the chip-label naming
// rule (same rule as the server's clone directory). Drift against the
// server means either a stuck submit button or an opaque 422.
describe("sandbox repository helpers", () => {
  it.each<[string, boolean]>([
    ["https://github.com/org/repo", true],
    ["https://github.com/org/repo.git", true],
    ["git@github.com:org/repo.git", true],
    // Bare shorthand and paths are not API surface.
    ["org/repo", false],
    ["/Users/me/repo", false],
    // Host with no repo path.
    ["https://github.com", false],
    ["", false],
    // Embedded fragment/whitespace belongs in the branch input, not here.
    ["https://github.com/org/repo#main", false],
    ["https://github.com/org/a repo", false],
  ])("isValidSandboxRepoUrl(%j) === %j", (url, expected) => {
    expect(isValidSandboxRepoUrl(url)).toBe(expected);
  });

  it.each<[string, string, string | undefined]>([
    // No repo → undefined, which JSON.stringify drops from the payload.
    ["", "", undefined],
    // Dangling branch without a URL also sends nothing (submit is
    // blocked separately, but compose must not invent "#main").
    ["", "main", undefined],
    ["https://github.com/org/repo", "", "https://github.com/org/repo"],
    ["https://github.com/org/repo", "main", "https://github.com/org/repo#main"],
    // Whitespace from pasting trims away on both parts.
    ["  https://github.com/org/repo  ", " main ", "https://github.com/org/repo#main"],
  ])("composeSandboxWorkspace(%j, %j) === %j", (url, branch, expected) => {
    expect(composeSandboxWorkspace(url, branch)).toBe(expected);
  });

  it.each<[string, string | null]>([
    ["https://github.com/org/repo", "repo"],
    // .git stripped — matches the server's clone-directory rule.
    ["https://github.com/org/repo.git", "repo"],
    ["git@github.com:org/repo.git", "repo"],
    ["https://github.com/org/repo/", "repo"],
    ["", null],
  ])("deriveRepoName(%j) === %j", (url, expected) => {
    expect(deriveRepoName(url)).toBe(expected);
  });
});

// deriveHomeDir resolves the working-directory default for a first-ever
// session on a host. It reads the parent of the first home-listing entry, so
// these pin the cases the seed depends on: a normal entry, a top-level entry,
// and the one case it can't resolve (empty home → null → blank field).
describe("deriveHomeDir", () => {
  it("returns the parent directory of the first entry", () => {
    expect(deriveHomeDir([fsEntry("/Users/corey/projects"), fsEntry("/Users/corey/Desktop")])).toBe(
      "/Users/corey",
    );
  });

  it("returns root for a top-level entry", () => {
    // A home directly under root (e.g. "/root") yields "/" — not "" — so the
    // seeded value is still a valid absolute path.
    expect(deriveHomeDir([fsEntry("/etc")])).toBe("/");
  });

  it("returns null for an empty listing", () => {
    // Nothing to take a parent of → caller leaves the field blank rather
    // than seeding a wrong path.
    expect(deriveHomeDir([])).toBeNull();
  });
});

// A failed POST /v1/sessions must surface a reason, not silently
// reset the button. These pin the message the screen shows.
describe("describeCreateError", () => {
  it("uses FastAPI's detail string", async () => {
    const res = fakeResponse(400, async () => ({ detail: "host is offline" }));
    expect(await describeCreateError(res)).toBe("host is offline");
  });

  it("uses a top-level message string", async () => {
    const res = fakeResponse(409, async () => ({ message: "name taken" }));
    expect(await describeCreateError(res)).toBe("name taken");
  });

  it("uses a nested error.message", async () => {
    const res = fakeResponse(422, async () => ({
      error: { message: "bad workspace" },
    }));
    expect(await describeCreateError(res)).toBe("bad workspace");
  });

  it("falls back to the status code for a non-JSON body", async () => {
    const res = fakeResponse(500, async () => {
      throw new Error("not json");
    });
    expect(await describeCreateError(res)).toBe("Couldn't create the session (HTTP 500).");
  });

  it("falls back to the status code for an unrecognized shape", async () => {
    const res = fakeResponse(503, async () => ({ weird: true }));
    expect(await describeCreateError(res)).toBe("Couldn't create the session (HTTP 503).");
  });
});

describe("harnessUnconfiguredOnHost", () => {
  const hostWith = (configured: Record<string, boolean> | null | undefined): Host =>
    ({
      host_id: "host_1",
      name: "laptop",
      owner: "alice",
      status: "online",
      configured_harnesses: configured,
    }) as Host;

  it("warns only on an explicit false from the host", () => {
    const host = hostWith({ "claude-sdk": true, codex: false });
    // Explicit false → warn; explicit true → no warning.
    expect(harnessUnconfiguredOnHost("codex", host)).toBe(true);
    expect(harnessUnconfiguredOnHost("claude-sdk", host)).toBe(false);
  });

  it("never warns when readiness is unknown", () => {
    // Older host build: no map at all → unknown, never warn.
    expect(harnessUnconfiguredOnHost("codex", hostWith(null))).toBe(false);
    expect(harnessUnconfiguredOnHost("codex", hostWith(undefined))).toBe(false);
    // Harness missing from the map → unknown spelling, never warn.
    expect(harnessUnconfiguredOnHost("some-future-harness", hostWith({ codex: false }))).toBe(
      false,
    );
    // No host selected (sandbox / nothing picked) → no warning.
    expect(harnessUnconfiguredOnHost("codex", undefined)).toBe(false);
    expect(harnessUnconfiguredOnHost("codex", null)).toBe(false);
    // Agent without a harness → nothing to warn about.
    expect(harnessUnconfiguredOnHost(null, hostWith({ codex: false }))).toBe(false);
  });
});

describe("matchSkillInvocation", () => {
  const SKILLS = [{ name: "review-pr" }, { name: "cross-review" }];

  it("matches a bundled skill and splits off the argument string", () => {
    expect(matchSkillInvocation("/review-pr 123 focus on auth", SKILLS)).toEqual({
      name: "review-pr",
      args: "123 focus on auth",
    });
  });

  it("matches a bare invocation with empty args", () => {
    expect(matchSkillInvocation("/cross-review", SKILLS)).toEqual({
      name: "cross-review",
      args: "",
    });
  });

  it("tolerates surrounding whitespace (the sanitized prompt is trimmed)", () => {
    expect(matchSkillInvocation("  /review-pr 123  ", SKILLS)).toEqual({
      name: "review-pr",
      args: "123",
    });
  });

  it("returns null for a command that matches no bundled skill", () => {
    // Unknown commands — including host-discovered skills the server can't
    // know pre-session — fall through to plain text, mirroring the
    // in-session composer.
    expect(matchSkillInvocation("/typo do something", SKILLS)).toBeNull();
  });

  it("is case-sensitive like the in-session composer's exact-name lookup", () => {
    expect(matchSkillInvocation("/Review-pr 123", SKILLS)).toBeNull();
  });

  it("returns null for plain text without a leading slash", () => {
    expect(matchSkillInvocation("review-pr 123", SKILLS)).toBeNull();
  });

  it("returns null for a path-shaped command token (file-path guard)", () => {
    // Shared isSlashCommandText guard: a "/" inside the COMMAND token means
    // it's a file path, not a command.
    expect(matchSkillInvocation("/etc/hosts", SKILLS)).toBeNull();
    expect(matchSkillInvocation("/usr/local do something", SKILLS)).toBeNull();
  });

  it("matches when the args carry slashes (paths, URLs)", () => {
    // Only the command token is path-guarded — args are free-form. This is
    // the natural shape for review-pr (a PR URL as the argument); rejecting
    // it was flagged in review as the guard being over-broad.
    expect(matchSkillInvocation("/review-pr src/foo.ts", SKILLS)).toEqual({
      name: "review-pr",
      args: "src/foo.ts",
    });
    expect(matchSkillInvocation("/review-pr https://github.com/org/repo/pull/123", SKILLS)).toEqual(
      {
        name: "review-pr",
        args: "https://github.com/org/repo/pull/123",
      },
    );
  });
});

function host(status: "online" | "offline", i = 1): Host {
  return { host_id: `host_${i}`, name: `machine-${i}`, owner: "me", status };
}

function mockHosts(hosts: Host[]) {
  useHostsMock.mockReturnValue({
    data: hosts,
  } as unknown as ReturnType<typeof useHosts>);
}

function mockAgents(agents: AvailableAgent[]) {
  useAvailableAgentsMock.mockReturnValue({
    data: agents,
  } as unknown as ReturnType<typeof useAvailableAgents>);
}

// Shared mock setup for the landing-screen tests: one online host (host_1,
// auto-selected), two agents (Claude Code default + Codex), inert
// directory-session / runner-health / filesystem stubs, and a persisted
// recent workspace so the working-directory field seeds to a known path.
function setupLandingMocks() {
  authenticatedFetchMock.mockReset();
  useHostsMock.mockReset();
  useAvailableAgentsMock.mockReset();
  useHostFilesystemMock.mockReset();
  useDirectorySessionsMock.mockReset();
  useRunnerHealthMock.mockReset();
  setOmnigentHostConfig({});
  localStorage.clear();
  // host_1's most-recent workspace seeds the field (so submit can enable
  // without manual picks). Tests that exercise the home fallback clear this.
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: ["/Users/corey/repo"] }));
  useDirectorySessionsMock.mockReturnValue({
    data: [],
  } as unknown as ReturnType<typeof useDirectorySessions>);
  useRunnerHealthMock.mockReturnValue(new Map<string, boolean>());
  useHostFilesystemMock.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
    isPlaceholderData: false,
  } as unknown as ReturnType<typeof useHostFilesystem>);
  mockHosts([host("online")]);
  mockAgents([
    {
      id: "a1",
      name: "claude-native-ui",
      display_name: "Claude Code",
      description: null,
      harness: "claude-native",
      skills: [],
    },
    {
      id: "a2",
      name: "codex-native-ui",
      display_name: "Codex",
      description: null,
      harness: "codex-native",
      skills: [],
    },
  ]);
}

function renderLanding(infoOverrides: Partial<ServerInfo> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const info: ServerInfo = {
    accounts_enabled: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    ...infoOverrides,
  };
  return render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={info}>
        <TooltipProvider>
          <MemoryRouter>
            <NewChatLandingScreen />
          </MemoryRouter>
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

describe("NewChatLandingScreen", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("renders the inline composer with the prompt headline", () => {
    renderLanding();
    // The home page offers an inline chat box rather than the old
    // "click New session in the sidebar" placeholder. If it regressed to
    // the placeholder, the composer input would be absent and this fails.
    expect(screen.getByText("What should we do?")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-input")).toBeTruthy();
  });

  it("enables submit only once a message, host, agent and valid workspace are set", async () => {
    renderLanding();
    const submit = screen.getByTestId("new-chat-landing-submit") as HTMLButtonElement;
    // Host (auto-selected) + agent (default) + workspace (seeded from the
    // recent) are all present, but with no message there's no task → disabled.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "   " },
    });
    // Whitespace-only is still empty after trim — button stays disabled.
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "inspect the repo" },
    });
    // Real text + the other gates satisfied → enabled. If canSubmit regressed
    // (e.g. dropped the workspace gate), the blank cases above would have
    // enabled too.
    expect(submit.disabled).toBe(false);
  });

  it("keeps submit disabled when no agents exist", () => {
    mockAgents([]);
    renderLanding();
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "do something" },
    });
    // No agent to bind the session to → submit stays disabled despite text.
    expect((screen.getByTestId("new-chat-landing-submit") as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.getByText("No agents")).toBeTruthy();
  });

  it("seeds the working directory from the host's most-recent path", async () => {
    renderLanding();
    // host_1's recent ("/Users/corey/repo") seeds the field; the chip shows
    // the basename. A regression in the seed effect leaves it "Working
    // directory" and submit stuck disabled.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
  });

  it("falls back to the host's home directory when there is no recent", async () => {
    // No recents for this host → the field seeds from the home listing
    // (parent of the first entry), so a first-ever session is still one click.
    localStorage.clear();
    useHostFilesystemMock.mockReturnValue({
      data: { entries: [fsEntry("/home/corey/projects")], truncated: false },
      isLoading: false,
      error: null,
      isPlaceholderData: false,
    } as unknown as ReturnType<typeof useHostFilesystem>);
    renderLanding();
    // deriveHomeDir("/home/corey/projects") → "/home/corey" → chip basename.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("corey"),
    );
  });

  it("opens the connect-host instructions from the host dropdown", () => {
    renderLanding();
    // Radix dropdowns open on pointerdown (a bare click doesn't in jsdom).
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-connect-host"));
    // The modal mounts the connect instructions with the runnable command.
    expect(screen.getByTestId("connect-host-dialog")).toBeTruthy();
    expect(screen.getByTestId("connect-host-command")).toBeTruthy();
  });

  it("offers connect-host even when no hosts are online (no dead end)", () => {
    mockHosts([]);
    renderLanding();
    // The chip reads the empty state…
    expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("No hosts");
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    // …and the connect item is still present, so a fresh user can unblock.
    expect(screen.getByTestId("new-chat-landing-connect-host")).toBeTruthy();
  });

  it("shows permission-mode options in the Advanced menu only for the claude-native agent", () => {
    renderLanding();
    // The radios live behind the footer tray's Advanced chip — absent
    // until the menu opens.
    expect(screen.queryByTestId("new-chat-landing-permission-plan")).toBeNull();
    // a1 (Claude Code, claude-native) is the default agent → the footer
    // tray surfaces the Advanced chip with the permission-mode radios.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-advanced-chip"), { button: 0 });
    const planOption = screen.getByTestId("new-chat-landing-permission-plan");
    expect(planOption.textContent).toContain("Plan");
    // The footer line explains the SELECTED mode until a row is hovered —
    // then it follows the hover, so every mode is explained without six
    // two-line rows.
    const detail = screen.getByTestId("new-chat-landing-permission-detail");
    expect(detail.textContent).toContain("Prompts before edits and commands");
    fireEvent.pointerEnter(planOption);
    expect(detail.textContent).toContain("Plans only; makes no edits");
    // Switch to Codex (a2: codex-native) — the Advanced chip stays visible
    // but now shows approval-mode radios instead of permission-mode radios.
    // Close the Advanced menu first (Escape), then switch agents.
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-a2"));
    expect(screen.queryByTestId("new-chat-landing-advanced-chip")).not.toBeNull();
  });

  it("shows approval-mode options in the Advanced menu for the codex-native agent", () => {
    renderLanding();
    // Switch to Codex first.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-a2"));
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-advanced-chip"), { button: 0 });
    const neverOption = screen.getByTestId("new-chat-landing-approval-never");
    expect(neverOption.textContent).toContain("Never");
    // The footer line explains the SELECTED mode until a row is hovered.
    const detail = screen.getByTestId("new-chat-landing-approval-detail");
    expect(detail.textContent).toContain("Model decides when to ask for approval");
    fireEvent.pointerEnter(neverOption);
    expect(detail.textContent).toContain("Never asks for approval");
  });

  it("shows a conflict banner in the file browser for an occupied directory", async () => {
    // A live session in the seeded workspace ("/Users/corey/repo") on the
    // auto-selected host occupies the directory the picker opens at.
    useDirectorySessionsMock.mockReturnValue({
      data: [conv({ id: "s1", host_id: "host_1", workspace: "/Users/corey/repo" })],
    } as unknown as ReturnType<typeof useDirectorySessions>);
    useRunnerHealthMock.mockReturnValue(new Map([["s1", true]]));
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    // The chip itself carries no warning — the guidance lives inside the
    // browser, on the folder you'd actually commit to.
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    const banner = screen.getByTestId("workspace-picker-conflict");
    // Singular copy proves the count (1) flowed through, not just that *some*
    // banner rendered.
    expect(banner.textContent).toContain("1 other agent is");
  });

  it("suppresses the conflict banner once a git branch is named", async () => {
    useDirectorySessionsMock.mockReturnValue({
      data: [conv({ id: "s1", host_id: "host_1", workspace: "/Users/corey/repo" })],
    } as unknown as ReturnType<typeof useDirectorySessions>);
    useRunnerHealthMock.mockReturnValue(new Map([["s1", true]]));
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    // Name a git branch: that starts an isolated worktree, so the picked
    // directory is no longer shared and the picker must not warn.
    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/x" },
    });
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    expect(screen.queryByTestId("workspace-picker-conflict")).toBeNull();
  });

  it("shows no conflict banner when no live session shares the directory", async () => {
    // Default setup: no other directory sessions → nothing to warn about.
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    expect(screen.queryByTestId("workspace-picker-conflict")).toBeNull();
  });

  it("opens the file browser directly from the working-directory chip", async () => {
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    // Clicking the chip shows the tree browser straight away — no intermediate
    // path-field + folder-button step. The old WorkspacePathField (its
    // `workspace-path-input`) and the browse toggle must be gone, and the
    // WorkspacePicker present.
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    expect(screen.getByTestId("workspace-picker")).toBeTruthy();
    expect(screen.queryByTestId("workspace-browse-toggle")).toBeNull();
    expect(screen.queryByTestId("workspace-path-input")).toBeNull();
  });

  it("updates the working-directory value live as you browse, with no Select button", async () => {
    // The picker lists a child folder under the seeded workspace.
    useHostFilesystemMock.mockReturnValue({
      data: { entries: [fsEntry("/Users/corey/repo/src")], truncated: false },
      isLoading: false,
      error: null,
      isPlaceholderData: false,
    } as unknown as ReturnType<typeof useHostFilesystem>);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    fireEvent.click(screen.getByTestId("new-chat-landing-workspace-chip"));
    // No explicit commit button — selection is live (closes on click-out).
    expect(screen.queryByTestId("workspace-picker-select")).toBeNull();
    // Clicking a folder navigates into it and updates the chip immediately,
    // without closing the popover.
    fireEvent.click(screen.getByTestId("workspace-picker-entry-src"));
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("src"),
    );
    expect(screen.getByTestId("workspace-picker")).toBeTruthy();
  });

  it("hides the sandbox option when the server doesn't support managed sandboxes", () => {
    // Default renderLanding: managed_sandboxes_enabled false (the fail-closed
    // probe sentinel). The dropdown must not advertise a create path the
    // server would reject with "managed hosts are not configured".
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    // connect-host proves the menu actually opened — without it, a closed
    // menu would make the absence assertion below pass vacuously.
    expect(screen.getByTestId("new-chat-landing-connect-host")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-sandbox-option")).toBeNull();
  });

  it("shows a disabled sandbox row with host-provided tooltip content when managed sandboxes are unavailable", async () => {
    setOmnigentHostConfig({
      docsLinks: { newSandbox: "Managed sandboxes are disabled in this workspace." },
    });
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    const disabledRow = screen.getByTestId("new-chat-landing-sandbox-option-disabled");
    expect(disabledRow).toBeTruthy();
    // Disabled helper row replaces the clickable sandbox option.
    expect(screen.queryByTestId("new-chat-landing-sandbox-option")).toBeNull();
    fireEvent.focus(screen.getByLabelText("Why New Sandbox is unavailable"));
    await waitFor(() =>
      expect(
        screen.getAllByText("Managed sandboxes are disabled in this workspace.").length,
      ).toBeGreaterThan(0),
    );
  });

  it("defaults to New Sandbox when the server supports managed sandboxes", async () => {
    // No clicks: the auto-select effect picks the FIRST menu option — the
    // sandbox — even though an online host (machine-1) exists. If this
    // regressed to host-first, the chip would read "machine-1".
    renderLanding({ managed_sandboxes_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("New Sandbox"),
    );
    // Sandbox mode chrome comes with the default: repository chip in,
    // workspace/worktree chips out.
    expect(screen.getByTestId("new-chat-landing-repo-chip")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-workspace-chip")).toBeNull();
  });

  it("labels the sandbox option with the server's provider name", async () => {
    // sandbox_provider drives the per-provider label. "modal" must read
    // "Modal Sandbox" on both the chip and the dropdown option — if the
    // label regressed to the generic "New Sandbox", the provider name
    // never reached the UI.
    renderLanding({ managed_sandboxes_enabled: true, sandbox_provider: "modal" });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain(
        "Modal Sandbox",
      ),
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-sandbox-option").textContent).toContain(
      "Modal Sandbox",
    );
  });

  it("defaults to New Sandbox when no hosts are connected and sandboxes are enabled", async () => {
    // The screenshot regression: zero hosts used to leave the chip stuck
    // on "No hosts" even though the sandbox option was one click away.
    mockHosts([]);
    renderLanding({ managed_sandboxes_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("New Sandbox"),
    );
    expect(screen.getByTestId("new-chat-landing-host-chip").textContent).not.toContain("No hosts");
  });

  it("switching between a host and the sandbox swaps the workspace chrome", async () => {
    renderLanding({ managed_sandboxes_enabled: true });
    // Sandbox is the default; switch to the host first so the test
    // exercises both directions of the toggle.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("New Sandbox"),
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    // The sandbox option is pinned FIRST in the menu, above the host list —
    // DOCUMENT_POSITION_FOLLOWING means the host item comes after it.
    const sandboxOption = screen.getByTestId("new-chat-landing-sandbox-option");
    const hostItem = screen
      .getAllByText("machine-1")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    expect(hostItem).toBeTruthy();
    expect(
      sandboxOption.compareDocumentPosition(hostItem!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Picking the host restores the workspace flow (file-browser chip,
    // worktree chip) — the sandbox default doesn't wedge the normal path.
    fireEvent.click(hostItem!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("machine-1"),
    );
    expect(screen.getByTestId("new-chat-landing-workspace-chip")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-branch-chip")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-repo-chip")).toBeNull();
    // And back: selecting the sandbox clears the host pick and swaps the
    // chips again. The auto-select effect must not override this either.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("New Sandbox"),
    );
    expect(screen.queryByTestId("new-chat-landing-workspace-chip")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-branch-chip")).toBeNull();
  });

  it("creates a managed session without host_id/workspace and no provisioning subtext", async () => {
    // Controlled promise so the in-flight state is observable
    // deterministically before the create resolves.
    let resolveCreate!: (res: Response) => void;
    authenticatedFetchMock.mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "audit the repo" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    // The managed create is non-blocking server-side and the session
    // page owns all launch progress — the landing page must NOT show
    // sandbox-specific pending copy (a regression here re-introduces
    // the "Provisioning sandbox…" subtext that delayed the perceived
    // navigation), and no error either.
    expect(screen.queryByTestId("new-chat-landing-provisioning")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-error")).toBeNull();
    // The payload is the managed shape: host_type only. host_id/workspace
    // would be 422-rejected by the server schema, and git requires host_id.
    const [url, init] = authenticatedFetchMock.mock.calls[0];
    expect(url).toBe("/v1/sessions");
    const body = JSON.parse((init as RequestInit).body as string) as Record<string, unknown>;
    expect(body.host_type).toBe("managed");
    expect(body.agent_id).toBe("a1");
    expect("host_id" in body).toBe(false);
    expect("workspace" in body).toBe(false);
    expect("git" in body).toBe(false);
    resolveCreate({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    // The resolved create navigates without surfacing an error.
    await waitFor(() => expect(screen.queryByTestId("new-chat-landing-error")).toBeNull());
  });

  it.each([
    {
      name: "not-configured OmnigentError",
      status: 400,
      body: { error: { message: "managed hosts are not configured on this server" } },
      expected: "managed hosts are not configured on this server",
    },
    {
      name: "online-poll timeout 502",
      status: 502,
      body: { detail: "managed host did not come online within 120s" },
      expected: "managed host did not come online within 120s",
    },
  ])("surfaces the $name from a failed managed create", async ({ status, body, expected }) => {
    authenticatedFetchMock.mockResolvedValue({
      ok: false,
      status,
      json: async () => body,
    } as unknown as Response);
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "audit the repo" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));
    // The server's message lands verbatim in the error line (via
    // describeCreateError), and the pending copy is gone — the user sees
    // why provisioning failed, not a silent reset.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-error").textContent).toContain(expected),
    );
    expect(screen.queryByTestId("new-chat-landing-provisioning")).toBeNull();
  });

  it("sends the repository inputs as the managed workspace string", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    // The repository chip replaces the file-browser workspace chip in
    // sandbox mode.
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-input"), {
      target: { value: "https://github.com/org/myrepo" },
    });
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-branch-input"), {
      target: { value: "release-1.2" },
    });
    // The chip reflects the pick using the server's clone-dir naming.
    expect(screen.getByTestId("new-chat-landing-repo-chip").textContent).toContain(
      "myrepo#release-1.2",
    );
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "audit the repo" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    const [, init] = authenticatedFetchMock.mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string) as Record<string, unknown>;
    // One composed string — the Docker-build-context-style form the
    // server parses and clones. host_id/git stay absent (422 otherwise).
    expect(body.workspace).toBe("https://github.com/org/myrepo#release-1.2");
    expect(body.host_type).toBe("managed");
    expect("host_id" in body).toBe(false);
    expect("git" in body).toBe(false);
  });

  it("shows host-provided git credentials tooltip content in the sandbox repo popover", async () => {
    setOmnigentHostConfig({
      docsLinks: { databricksGitCredentials: "Use Databricks Git credentials before cloning." },
    });
    renderLanding({ managed_sandboxes_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("New Sandbox"),
    );
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    const helpButton = screen.getByLabelText("How to set up Databricks git credentials");
    expect(helpButton).toBeTruthy();
    fireEvent.focus(helpButton);
    await waitFor(() =>
      expect(
        screen.getAllByText("Use Databricks Git credentials before cloning.").length,
      ).toBeGreaterThan(0),
    );
  });

  it("blocks submit on an invalid repository URL or a dangling branch", () => {
    renderLanding({ managed_sandboxes_enabled: true });
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "do something" },
    });
    const submit = screen.getByTestId("new-chat-landing-submit") as HTMLButtonElement;
    // No repo at all is a valid sandbox create (empty workspace).
    expect(submit.disabled).toBe(false);
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    // A branch with no repository is dangling — nothing to clone it from.
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-branch-input"), {
      target: { value: "main" },
    });
    expect(submit.disabled).toBe(true);
    // An unusable URL shape would 422 server-side; gate it inline.
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-input"), {
      target: { value: "org/repo" },
    });
    expect(submit.disabled).toBe(true);
    // Completing a valid URL re-enables submit.
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-input"), {
      target: { value: "https://github.com/org/repo" },
    });
    expect(submit.disabled).toBe(false);
  });
});

// The landing composer's "/" skills menu: bundled skills of the chosen
// agent surface as suggestions before any session exists, so a skill can
// be invoked from the very first message. Native terminal agents are
// excluded — their CLI owns slash commands.
describe("NewChatLandingScreen skills menu", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  /** A non-native agent carrying two bundled skills. */
  function skilledAgent(): AvailableAgent {
    return {
      id: "ag_skilled",
      name: "skilled-agent",
      display_name: "Skilled Agent",
      description: null,
      harness: "claude-sdk",
      skills: [
        { name: "review-pr", description: "Review a pull request" },
        { name: "cross-review", description: "Cross-vendor review" },
      ],
    };
  }

  function typeMessage(text: string) {
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: text },
    });
  }

  it("lists the chosen agent's bundled skills when the draft starts with /", () => {
    mockAgents([skilledAgent()]);
    renderLanding();
    typeMessage("/");
    // Both bundled skills render as rows under the "Skills" section header
    // — proving the menu reads skills off GET /v1/agents (the only source
    // here; there is no session snapshot yet). Row testids, not text: the
    // active entry's name also renders in the detail card.
    expect(screen.getByText("Skills")).toBeTruthy();
    expect(screen.getByTestId("slash-menu-item-review-pr")).toBeTruthy();
    expect(screen.getByTestId("slash-menu-item-cross-review")).toBeTruthy();
    // Descriptions live in the detail card beside the panel and follow the
    // highlight: the pre-selected first row's blurb shows, the other's
    // doesn't until ArrowDown moves the highlight.
    expect(screen.getByText("Review a pull request")).toBeTruthy();
    expect(screen.queryByText("Cross-vendor review")).toBeNull();
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-input"), { key: "ArrowDown" });
    expect(screen.getByText("Cross-vendor review")).toBeTruthy();
  });

  it("filters by the typed query and fills the draft on click", () => {
    mockAgents([skilledAgent()]);
    renderLanding();
    typeMessage("/rev");
    // The query narrows the list to the prefix match only.
    expect(screen.getByTestId("slash-menu-item-review-pr")).toBeTruthy();
    expect(screen.queryByTestId("slash-menu-item-cross-review")).toBeNull();
    fireEvent.click(screen.getByTestId("slash-menu-item-review-pr"));
    // Selection fills "/name " (trailing space, caret ready for args) —
    // skills never auto-submit from the menu.
    expect((screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement).value).toBe(
      "/review-pr ",
    );
  });

  it("completes the highlighted skill with Tab instead of submitting", () => {
    mockAgents([skilledAgent()]);
    renderLanding();
    typeMessage("/rev");
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-input"), { key: "Tab" });
    // The first match is pre-selected on open, so Tab completes it without
    // arrowing down first (same UX as the in-session composer).
    expect((screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement).value).toBe(
      "/review-pr ",
    );
  });

  it("closes the menu once the command name is complete (space typed)", () => {
    mockAgents([skilledAgent()]);
    renderLanding();
    typeMessage("/review-pr 123");
    // A space means the name is done and args follow — suggestions go away.
    expect(screen.queryByText("Review a pull request")).toBeNull();
  });

  it("shows no menu for native terminal agents even if skills are listed", () => {
    // A native agent with (hypothetical) bundled skills: the gate is the
    // agent kind, not an empty skill list — the vendor CLI interprets
    // slash commands itself, so the web menu must stay out of the way.
    mockAgents([
      {
        id: "a1",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      },
    ]);
    renderLanding();
    typeMessage("/");
    expect(screen.queryByTestId("slash-menu-item-review-pr")).toBeNull();
  });
});

// Always-visible skill pills under the landing composer for allowlisted
// orchestrators (polly/debby): pills surface bundled skills without
// typing "/", and clicking one prefills the composer — it never sends.
describe("NewChatLandingScreen skill pills", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  /** Debby — allowlisted for pills, carrying two bundled skills. */
  function debbyAgent(): AvailableAgent {
    return {
      id: "ag_debby",
      name: "debby",
      display_name: "Debby",
      description: "Multi-agent debate",
      harness: "claude-sdk",
      skills: [
        { name: "debate", description: "Have both heads argue it out" },
        { name: "compare", description: "Side-by-side answers from both heads" },
      ],
    };
  }

  function input(): HTMLTextAreaElement {
    return screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement;
  }

  it("renders bundled skills as pills without typing anything", () => {
    mockAgents([debbyAgent()]);
    renderLanding();
    // Both pills render on a pristine screen — proving the pills are
    // always-visible (not gated on a "/" draft like the slash menu) and
    // fed from GET /v1/agents bundled skills.
    expect(screen.getByTestId("skill-pill-debate").textContent).toBe("/debate");
    expect(screen.getByTestId("skill-pill-compare").textContent).toBe("/compare");
  });

  it("hides pills for agents outside the allowlist even when they carry skills", () => {
    // Same skills, non-allowlisted name: no pill row. Fails if the gate
    // ever degrades to "any agent with skills", which would spam the
    // landing screen for every custom agent.
    mockAgents([
      {
        id: "ag_other",
        name: "skilled-agent",
        display_name: "Skilled Agent",
        description: null,
        harness: "claude-sdk",
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      },
    ]);
    renderLanding();
    expect(screen.queryByTestId("skill-pills")).toBeNull();
  });

  it("appears when the user switches the picker to an allowlisted agent", () => {
    // Claude Code ranks first (AGENT_DISPLAY_ORDER), so debby is NOT the
    // default selection — no pills until the user picks her. This is the
    // core interaction: click debby in the picker, her skills appear.
    mockAgents([
      {
        id: "a1",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
      },
      debbyAgent(),
    ]);
    renderLanding();
    expect(screen.queryByTestId("skill-pills")).toBeNull();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_debby"));
    expect(screen.getByTestId("skill-pill-debate")).toBeTruthy();
  });

  it("fills '/name ' into an empty draft on click without sending", () => {
    mockAgents([debbyAgent()]);
    renderLanding();
    fireEvent.click(screen.getByTestId("skill-pill-debate"));
    // Trailing space = caret ready for args; pills never auto-execute
    // (same contract as picking from the "/" menu).
    expect(input().value).toBe("/debate ");
  });

  it("hides the prompt text and pills once the user types", () => {
    mockAgents([debbyAgent()]);
    renderLanding();
    // Pristine empty draft: the prompt text and the pills share the first
    // line as one affordance.
    expect(screen.getByText("Describe a task, or try a skill")).toBeTruthy();
    expect(screen.getByTestId("skill-pill-debate")).toBeTruthy();
    // The instant a draft exists the whole empty-state affordance
    // collapses — both the prompt text and the pills yield to the user's
    // text so neither overlaps what they're typing.
    fireEvent.change(input(), { target: { value: "h" } });
    expect(screen.queryByText("Describe a task, or try a skill")).toBeNull();
    expect(screen.queryByTestId("skill-pills")).toBeNull();
  });

  it("shows the skill description bubble on focus, like the / menu detail card", () => {
    mockAgents([debbyAgent()]);
    renderLanding();
    // Description is nowhere in the DOM until the pill is focused/hovered.
    expect(screen.queryByText("Have both heads argue it out")).toBeNull();
    fireEvent.focus(screen.getByTestId("skill-pill-debate"));
    // getAllBy: radix mounts the open tooltip twice (portal content + a
    // visually-hidden a11y copy) — both carry the description.
    expect(screen.getAllByText("Have both heads argue it out").length).toBeGreaterThan(0);
  });
});

// Attachments on the landing composer — same paperclip affordance as the
// in-session composer; files ride the pending-prompt handoff (covered in
// the flow tests), this suite covers the local chip UI.
describe("NewChatLandingScreen attachments", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("attaches files via the paperclip input and removes them via the chip", () => {
    renderLanding();
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
      target: { files: [file] },
    });
    // Chip shows the filename — proves the file landed in state, not just
    // that the input fired.
    expect(screen.getByText("notes.txt")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    expect(screen.queryByText("notes.txt")).toBeNull();
  });

  it("attaches files dropped onto the composer and surfaces a drop overlay", () => {
    renderLanding();
    const composer = screen.getByTestId("new-chat-landing-composer");
    // Dragging over the composer lifts the drop-target overlay.
    fireEvent.dragOver(composer, { dataTransfer: { files: [] } });
    expect(screen.getByText("Drop files here")).toBeTruthy();
    // Dropping a file attaches it (chip proves it reached state) and clears
    // the overlay.
    const file = new File(["hello"], "dropped.txt", { type: "text/plain" });
    fireEvent.drop(composer, { dataTransfer: { files: [file] } });
    expect(screen.getByText("dropped.txt")).toBeTruthy();
    expect(screen.queryByText("Drop files here")).toBeNull();
  });

  it("clears the drop overlay when the drag leaves the composer", () => {
    renderLanding();
    const composer = screen.getByTestId("new-chat-landing-composer");
    fireEvent.dragEnter(composer, { dataTransfer: { files: [] } });
    expect(screen.getByText("Drop files here")).toBeTruthy();
    // relatedTarget defaults to null (outside the composer), so the active
    // state clears rather than sticking when moving between child elements.
    fireEvent.dragLeave(composer, { dataTransfer: { files: [] } });
    expect(screen.queryByText("Drop files here")).toBeNull();
  });
});
