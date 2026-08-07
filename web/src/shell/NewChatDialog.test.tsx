import type * as IdentityModule from "@/lib/identity";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as ChatStoreModule from "@/store/chatStore";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import {
  composeSandboxWorkspace,
  deriveHomeDir,
  deriveRepoName,
  describeCreateError,
  displayNameForHost,
  harnessUnavailableReasonOnHost,
  harnessUnconfiguredOnHost,
  isValidSandboxRepoUrl,
  isValidWorkspace,
  matchSkillInvocation,
  normalizeWorkspacePath,
  resolveThisMachineHostId,
  sessionsSharingDirectory,
  worktreePathTail,
  NewChatLandingScreen,
  resetLandingDraft,
} from "./NewChatDialog";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import {
  useHostModelOptions,
  useHosts,
  useInstallHarness,
  useInstallingHarnesses,
  type Host,
} from "@/hooks/useHosts";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import type { Conversation } from "@/hooks/useConversations";
import { setOmnigentHostConfig } from "@/lib/host";
import { writeHideUnconfiguredHarnesses } from "@/lib/harnessVisibilityPreferences";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { TooltipProvider } from "@/components/ui/tooltip";

// Only authenticatedFetch is stubbed (the create POST under test);
// the module's other exports stay real for any other consumer in the tree.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(),
  // The setup dialog mounts these; default to inert so tests that don't
  // exercise install / credential-write don't need to wire them up.
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
  useStoreCredential: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useDetectedCredentials: vi.fn(() => ({ data: [] })),
}));
// The setup dialog's copyable command rows call copyText; stub it so a click
// can be asserted without touching the real clipboard.
const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
// The install flow surfaces its result via a toast; capture the text so the
// "ready vs. one-more-step" wording can be asserted.
const { showToastMock } = vi.hoisted(() => ({ showToastMock: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: showToastMock }));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
  // WorkspacePicker (rendered by the file browser) reads this on mount;
  // an idle mutation keeps it inert for these tests.
  useCreateHostDirectory: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}));
// Mocked so it doesn't hit authenticatedFetch (which would pollute the
// call list the create-flow assertions index into positionally).
vi.mock("@/hooks/useHostWorktrees", () => ({ useHostWorktrees: vi.fn() }));
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: vi.fn(),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: vi.fn(),
}));
// The composer's project chip lists projects via useProjects; stub it to an
// empty list so it doesn't fire its own authenticatedFetch (which would skew
// the create-POST call-count / call-order assertions below).
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  // Empty projects list → no ?project= name resolves to an id, so the project
  // prefill stays inert and the generic host/workspace defaults under test apply.
  useProjects: () => ({ data: [] }),
}));
// The harness-label catalog is not under test here. Keep it synchronous so
// create-session fetch assertions only observe the POST/PATCH calls they own.
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  // Mirrors the real hook: the "auto" sentinel leads the map only when the
  // server enables routing, so the Auto-harness tests exercise the real gate.
  useBrainHarnessLabels: (smartRoutingEnabled = false) => ({
    ...(smartRoutingEnabled ? { auto: "Smart Routing" } : {}),
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
    copilot: "Copilot",
  }),
  // The setup dialog reads server-authored steps from here; stub codex-native's
  // two-step flow (install → login) so the dialog renders without a real fetch.
  useHarnessSetupSteps: () => ({
    "codex-native": [
      {
        kind: "install",
        title: "Install Codex",
        detail: "We'll install Codex on the host for you.",
        action: "install",
        command: null,
        status_key: "installed",
      },
      {
        kind: "auth",
        title: "Set up authentication",
        detail: "Sign in with your ChatGPT subscription, an API key, or a gateway.",
        action: "auth",
        command: "codex login",
        status_key: "authed",
      },
    ],
  }),
}));
// Partial mock: only spy on the first-message handoff so the "@"-mention
// tests can assert the prepended attachment marker. Everything else
// (composerAttachmentKey, useChatStore, …) stays real for the render tree.
vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  setPendingInitialPrompt: vi.fn(),
}));

const authenticatedFetchMock = vi.mocked(authenticatedFetch);
const useHostsMock = vi.mocked(useHosts);
/** Stable per-harness model-catalog results (identity matters: effects key on them). */
const CLAUDE_MODEL_OPTIONS_RESULT = {
  data: [
    { id: "opus", model: "system.ai.claude-opus-4-8[1m]", displayName: "Opus 4.8" },
    { id: "sonnet", model: "system.ai.claude-sonnet-4-6[1m]", displayName: "Sonnet 4.6" },
    { id: "haiku", model: "system.ai.claude-haiku-4-5", displayName: "Haiku 4.5" },
  ],
  isLoading: false,
  isError: false,
};
const CODEX_MODEL_OPTIONS_RESULT = {
  data: [
    { id: "databricks-gpt-5-5", displayName: "GPT-5.5", isDefault: true },
    { id: "databricks-gpt-5-6", displayName: "GPT-5.6" },
  ],
  isLoading: false,
  isError: false,
};

const useHostModelOptionsMock = vi.mocked(useHostModelOptions);
const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const useHostWorktreesMock = vi.mocked(useHostWorktrees);
const useDirectorySessionsMock = vi.mocked(useDirectorySessions);
const useRunnerHealthMock = vi.mocked(useRunnerHealthRegistration);
const setPendingInitialPromptMock = vi.mocked(setPendingInitialPrompt);

const RECENT_KEY = "omnigent:recent-workspaces";
// Per-harness remembered option knobs (see lib/modePreferences).
const HARNESS_OPTIONS_KEY = "omnigent:last-mode-by-harness";
// Last harness pick per agent (see lib/harnessPreferences).
const LAST_HARNESS_KEY = "omnigent:last-harness-by-agent";
// Last agent pick (see lib/agentPreferences).
const LAST_AGENT_KEY = "omnigent:last-agent-id";

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

describe("displayNameForHost", () => {
  const local = { host_id: "host_local", name: "HR4V76FMWY" };

  it("uses an OS-friendly name for a matching host and falls back to its hostname", () => {
    expect(displayNameForHost(local, "host_local", "Mozilla/5.0 (Macintosh)")).toBe("This Mac");
    expect(displayNameForHost(local, "host_local", "Mozilla/5.0 (Windows NT 10.0)")).toBe(
      "This Windows",
    );
    expect(displayNameForHost(local, "host_local", "Mozilla/5.0 (X11; Linux x86_64)")).toBe(
      "This machine",
    );
    expect(
      displayNameForHost(local, "host_local", "Mozilla/5.0 (Linux; Android 15; Pixel 9)"),
    ).toBe("This Android");
    expect(
      displayNameForHost(
        local,
        "host_local",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
      ),
    ).toBe("This iPhone");
    expect(displayNameForHost(local, "host_local", "Unknown client")).toBe("HR4V76FMWY");
    expect(displayNameForHost(local, "host_other", "Mozilla/5.0 (Macintosh)")).toBe("HR4V76FMWY");
    expect(displayNameForHost(local, null, "Mozilla/5.0 (Macintosh)")).toBe("HR4V76FMWY");
  });
});

describe("resolveThisMachineHostId", () => {
  it("prefers Electron identity and only infers a browser host for local single-host servers", () => {
    expect(resolveThisMachineHostId("host_shell", true, ["host_online"])).toBe("host_shell");
    expect(resolveThisMachineHostId(null, true, ["host_online"])).toBe("host_online");
    expect(resolveThisMachineHostId(null, true, ["host_a", "host_b"])).toBeNull();
    expect(resolveThisMachineHostId(null, false, ["host_online"])).toBeNull();
  });
});

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

  it.each<[string, string]>([
    // Deep path → leading ellipsis + last two segments (the disambiguating tail).
    ["/Users/me/myrepo-worktrees/feature-x", "…/myrepo-worktrees/feature-x"],
    // Two-or-fewer segments → returned unchanged (nothing useful to trim).
    ["/Users", "/Users"],
    ["/a/b", "/a/b"],
    // Trailing slash doesn't create an empty tail segment.
    ["/Users/me/myrepo-worktrees/feature-x/", "…/myrepo-worktrees/feature-x"],
  ])("worktreePathTail(%j) === %j", (path, expected) => {
    expect(worktreePathTail(path)).toBe(expected);
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
  const hostWith = (configured: Record<string, boolean | string> | null | undefined): Host =>
    ({
      host_id: "host_1",
      name: "laptop",
      owner: "alice",
      status: "online",
      configured_harnesses: configured,
    }) as Host;

  it("warns only on an explicit false from the host", () => {
    const testHost = hostWith({ "claude-sdk": true, codex: false });
    // Explicit false → warn; explicit true → no warning.
    expect(harnessUnconfiguredOnHost("codex", testHost)).toBe(true);
    expect(harnessUnconfiguredOnHost("claude-sdk", testHost)).toBe(false);
  });

  it("classifies structured codex reasons (bare + native spellings)", () => {
    const testHost = hostWith({ codex: "needs-auth", "codex-native": "binary-missing" });
    expect(harnessUnconfiguredOnHost("codex", testHost)).toBe(true);
    expect(harnessUnavailableReasonOnHost("codex", testHost)).toBe("needs-auth");
    expect(harnessUnavailableReasonOnHost("codex-native", testHost)).toBe("binary-missing");
  });

  it("falls back to a generic warning for unknown reason strings", () => {
    expect(harnessUnavailableReasonOnHost("codex", hostWith({ codex: "future-reason" }))).toBe(
      "unconfigured",
    );
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
  useHostModelOptionsMock.mockReset();
  useAvailableAgentsMock.mockReset();
  useHostFilesystemMock.mockReset();
  useHostWorktreesMock.mockReset();
  useDirectorySessionsMock.mockReset();
  useRunnerHealthMock.mockReset();
  // Reset the install hooks to their inert defaults: per-test overrides
  // (a pending install set, a callback-firing mutate) must not leak into the
  // next test — a stale pending set would disable the Install button and make
  // a later click a silent no-op.
  vi.mocked(useInstallHarness).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof useInstallHarness>);
  vi.mocked(useInstallingHarnesses).mockReturnValue(new Set<string>());
  setOmnigentHostConfig({});
  resetLandingDraft();
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
  useHostWorktreesMock.mockReturnValue({
    data: undefined,
  } as unknown as ReturnType<typeof useHostWorktrees>);
  mockHosts([host("online")]);
  useHostModelOptionsMock.mockImplementation(
    (_hostId, harness) =>
      (harness === "codex-native"
        ? CODEX_MODEL_OPTIONS_RESULT
        : CLAUDE_MODEL_OPTIONS_RESULT) as unknown as ReturnType<typeof useHostModelOptions>,
  );
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

function renderLanding(infoOverrides: Partial<ServerInfo> = {}, route = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const info: ServerInfo = {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    // Default stub: the external AI-Gateway router alone, which is the world the
    // gateway-gating cases below were written against (off-gateway family → no
    // routing). Cases that exercise the built-in judge pass the field
    // explicitly.
    smart_routing_sources: { external: infoOverrides.smart_routing_enabled === true, oss: false },
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
    ...infoOverrides,
  };
  return render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={info}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[route]}>
            <NewChatLandingScreen />
          </MemoryRouter>
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

/** Open the picker and commit (select + close) an agent by clicking its row. */
/** Drop the mounted landing screen + its in-memory draft, keeping localStorage. */
function remountLanding(infoOverrides: Partial<ServerInfo> = {}): void {
  cleanup();
  resetLandingDraft();
  renderLanding(infoOverrides);
}

/**
 * Type *prompt* into the landing composer, submit, and read the create call.
 *
 * Returns both spellings the payload assertions need: the parsed body for
 * field checks and the raw JSON so "no field of any spelling rode along"
 * negative assertions can substring-search it.
 */
async function submitAndReadBody(
  prompt = "ship it",
): Promise<{ raw: string; body: Record<string, unknown> }> {
  fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
    target: { value: prompt },
  });
  fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
  return readCreateBody();
}

/** Wait for the create POST and read its raw + parsed body. */
async function readCreateBody(): Promise<{ raw: string; body: Record<string, unknown> }> {
  await waitFor(() =>
    expect(
      authenticatedFetchMock.mock.calls.some(
        ([url, init]) => url === "/v1/sessions" && (init as RequestInit | undefined)?.body,
      ),
    ).toBe(true),
  );
  const call = authenticatedFetchMock.mock.calls.find(([url]) => url === "/v1/sessions")!;
  const raw = (call[1] as RequestInit).body as string;
  return { raw, body: JSON.parse(raw) as Record<string, unknown> };
}

function selectAgent(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
}

/**
 * Select a harness that may live under the picker's "More" submenu. Fully
 * supported harnesses list inline even when they need setup; everything else
 * folds into "More". Drills in only when the row isn't already inline, so
 * callers don't have to track which side of the split their fixture lands on.
 */
function selectUnconfiguredAgent(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
  }
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
}

/**
 * Select <agentId> and open its run-config modal via the composer gear icon.
 * The knobs (model / effort / permission / approval / cursor mode / brain
 * harness) live in this modal, not the picker dropdown.
 */
function openAgentConfig(agentId: string): void {
  selectAgent(agentId);
  fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
}

/** Open a Radix Select trigger (opens on pointerdown in jsdom). */
function openSelect(testId: string): void {
  fireEvent.pointerDown(screen.getByTestId(testId), { button: 0 });
  fireEvent.click(screen.getByTestId(testId));
}

/** Open the config-modal Select at <triggerTestId> and click the option labeled <label>. */
function pickSelectOption(triggerTestId: string, label: string): void {
  openSelect(triggerTestId);
  fireEvent.click(screen.getByText(label));
}

/** Dismiss any open menu. */
function closeMenu(): void {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
}

/** Close the config modal by clicking Save (commits the draft). */
function saveConfig(): void {
  fireEvent.click(screen.getByTestId("new-chat-landing-config-save"));
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
    expect(screen.getByTestId("new-chat-landing-input")).toBeTruthy();
  });

  it("uses a home-specific focus shadow without a resting shadow or focus border", () => {
    renderLanding();

    const composer = screen.getByTestId("new-chat-landing-composer");
    expect(composer).toHaveClass(
      "border-border",
      "has-[textarea:focus]:shadow-[var(--composer-shadow-focus)]",
    );
    expect(composer).not.toHaveClass("shadow-[var(--composer-shadow)]");
    expect(composer.className).not.toContain("has-[textarea:focus]:border-");
  });

  it("matches the session composer internal padding", () => {
    renderLanding();

    expect(screen.getByTestId("new-chat-landing-input")).toHaveClass(
      "min-h-[60px]",
      "max-h-[200px]",
      "px-4",
      "pt-3",
      "pb-2",
    );
    expect(screen.getByTestId("new-chat-landing-actions")).toHaveClass("px-2", "pb-2");
    const footer = screen.getByTestId("new-chat-landing-footer");
    expect(footer).toHaveClass("py-1.5", "pr-4", "pl-2");
    expect(footer).not.toHaveClass("-mt-4");
    expect(footer.parentElement).toHaveClass("gap-1");
  });

  it("preserves the typed message and attachments when the landing screen unmounts and remounts", () => {
    // Navigating into an existing session and back unmounts the landing
    // screen; the draft is stashed at module scope so the half-composed
    // message and its attachments survive the round-trip instead of being
    // discarded.
    const first = renderLanding();
    const box = screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "half-typed thought" } });
    const file = new File(["data"], "diagram.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
      target: { files: [file] },
    });
    expect(screen.getByText("diagram.png")).toBeTruthy();
    first.unmount();

    renderLanding();
    expect((screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement).value).toBe(
      "half-typed thought",
    );
    // The attachment chip re-renders from the restored draft.
    expect(screen.getByText("diagram.png")).toBeTruthy();
  });

  it("hands the draft back when a create the user walked away from is rejected", async () => {
    // A submitted draft is dropped on unmount — it belongs to the session
    // being created. But a create the server rejects makes no session, so
    // the message is the user's again and must survive the round-trip
    // instead of vanishing with the failed attempt.
    let rejectCreate: (() => void) | null = null;
    authenticatedFetchMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          rejectCreate = () =>
            resolve({
              ok: false,
              status: 400,
              json: async () => ({ detail: "workspace already in use" }),
              text: async () => "workspace already in use",
            } as unknown as Response);
        }),
    );
    const first = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "rebuild the parser" },
    });
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(rejectCreate).not.toBeNull());

    // The user gives up waiting and opens another session, then the create
    // comes back rejected.
    first.unmount();
    await act(async () => {
      rejectCreate!();
    });

    renderLanding();
    expect((screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement).value).toBe(
      "rebuild the parser",
    );
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

  it("orders native built-ins together in the agent picker", () => {
    mockAgents([
      {
        id: "a_pi",
        name: "pi-native-ui",
        display_name: "Pi",
        description: null,
        harness: "pi-native",
        skills: [],
      },
      {
        id: "a_kiro",
        name: "kiro-native-ui",
        display_name: "Kiro",
        description: null,
        harness: "kiro-native",
        skills: [],
      },
      {
        id: "a_cursor",
        name: "cursor-native-ui",
        display_name: "Cursor",
        description: null,
        harness: "cursor-native",
        skills: [],
      },
      {
        id: "a_codex",
        name: "codex-native-ui",
        display_name: "Codex",
        description: null,
        harness: "codex-native",
        skills: [],
      },
      {
        id: "a_claude",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
      },
      {
        id: "a_polly",
        name: "polly",
        display_name: "Polly",
        description: null,
        harness: "claude-sdk",
        skills: [],
      },
    ]);
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    // Only the fully supported harnesses lead, in rank order, ahead of the
    // Agents group.
    const claude = screen.getByTestId("new-chat-landing-agent-a_claude");
    const codex = screen.getByTestId("new-chat-landing-agent-a_codex");
    const polly = screen.getByTestId("new-chat-landing-agent-a_polly");
    expect(claude.compareDocumentPosition(codex) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(codex.compareDocumentPosition(polly) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Every other harness folds into "More", even though all are configured
    // here — support level, not host readiness, decides the split.
    for (const id of ["a_cursor", "a_pi", "a_kiro"]) {
      expect(screen.queryByTestId(`new-chat-landing-agent-${id}`)).toBeNull();
    }
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
    const morePi = screen.getByTestId("new-chat-landing-agent-a_pi");
    const moreCursor = screen.getByTestId("new-chat-landing-agent-a_cursor");
    const moreKiro = screen.getByTestId("new-chat-landing-agent-a_kiro");
    // "More" keeps the same rank ordering: Cursor (30) → Pi (40) → Kiro (50).
    expect(
      moreCursor.compareDocumentPosition(morePi) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      morePi.compareDocumentPosition(moreKiro) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("pins a not-fully-supported harness inline once it is the selected pick", () => {
    // Pi isn't fully supported, so a fresh picker never leads with it — but
    // selecting it must pin it inline so the active pick is never buried.
    mockAgents([
      {
        id: "a_claude",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
      },
      {
        id: "a_pi",
        name: "pi-native-ui",
        display_name: "Pi",
        description: null,
        harness: "pi-native",
        skills: [],
      },
    ]);
    renderLanding();
    selectUnconfiguredAgent("a_pi");
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    // Now selected → inline, no drill-down needed.
    expect(screen.getByTestId("new-chat-landing-agent-a_pi")).toBeTruthy();
  });

  // claude-native (a1, fully supported) plus cursor-native (a_cursor, not) so
  // the preference has a "More" row to act on: the host below reports
  // cursor-native as unconfigured on this machine.
  function mockHostWithHarnessReadiness() {
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
        id: "a_cursor",
        name: "cursor-native-ui",
        display_name: "Cursor",
        description: null,
        harness: "cursor-native",
        skills: [],
      },
    ]);
    mockHosts([
      {
        ...host("online"),
        configured_harnesses: { "claude-native": true, "cursor-native": false },
      } as Host,
    ]);
  }

  it("keeps unconfigured harnesses in the 'More' submenu by default (opt-in preference off)", () => {
    // With the preference untouched the picker keeps offering harnesses that
    // aren't set up on the host: they stay in "More" (badged, not hidden), so
    // users can still discover and configure them.
    mockHostWithHarnessReadiness();
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-agent-a_cursor")).toBeNull();
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
    expect(screen.getByTestId("new-chat-landing-agent-a_cursor")).toBeTruthy();
  });

  it("hides harnesses unconfigured on the selected host when the preference is on", () => {
    // Preference on → cursor-native (unconfigured on host_1) drops out of the
    // picker entirely, taking the now-empty "More" trigger with it, while
    // claude-native (configured, fully supported) stays inline.
    writeHideUnconfiguredHarnesses(true);
    mockHostWithHarnessReadiness();
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-agent-a_cursor")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-harness-more")).toBeNull();
  });

  it("leads with fully supported harnesses even when they need setup on the host", () => {
    // Support level outranks readiness for the primary list: an unconfigured
    // Codex still leads (badged), rather than being demoted to "More".
    mockHosts([
      {
        ...host("online"),
        configured_harnesses: { "claude-native": true, "codex-native": false },
      } as Host,
    ]);
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-agent-a2")).toBeTruthy();
    // Nothing left to group → no "More" trigger at all.
    expect(screen.queryByTestId("new-chat-landing-harness-more")).toBeNull();
  });

  // Polly is a bundle agent whose brain harness (claude-sdk) is overridable, so
  // its config modal lists every brain harness — each badged when unconfigured.
  function mockPollyWithBrainReadiness() {
    mockHosts([
      {
        ...host("online"),
        configured_harnesses: {
          "claude-sdk": true,
          codex: "binary-missing",
          cursor: false,
          pi: false,
          antigravity: true,
          copilot: false,
        },
      } as Host,
    ]);
    mockAgents([
      {
        id: "a_polly",
        name: "polly",
        display_name: "Polly",
        description: null,
        harness: "claude-sdk",
        skills: [],
      },
    ]);
  }

  it("lists every brain harness in a bundle agent's override select by default", () => {
    // Preference off → the brain override still offers unconfigured harnesses
    // (badged), so they remain discoverable.
    mockPollyWithBrainReadiness();
    renderLanding();
    openAgentConfig("a_polly");
    openSelect("new-chat-landing-config-harness");
    expect(screen.getByTestId("new-chat-landing-harness-codex")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-harness-cursor")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-harness-copilot")).toBeTruthy();
  });

  it("hides unconfigured brain harnesses in the override select when the preference is on", () => {
    // Preference on → only brains that can launch on the host remain, plus the
    // selected default (claude-sdk) which always stays for coherence.
    writeHideUnconfiguredHarnesses(true);
    mockPollyWithBrainReadiness();
    renderLanding();
    openAgentConfig("a_polly");
    openSelect("new-chat-landing-config-harness");
    expect(screen.getByTestId("new-chat-landing-harness-claude-sdk")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-harness-antigravity")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-harness-codex")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-harness-cursor")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-harness-pi")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-harness-copilot")).toBeNull();
  });

  // Harnesses the user has actually launched are promoted out of "More" into
  // the primary list, so a regular Pi / Cursor user gets theirs one click away.
  function mockClaudeAndPi() {
    mockAgents([
      {
        id: "a_claude",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
      },
      {
        id: "a_pi",
        name: "pi-native-ui",
        display_name: "Pi",
        description: null,
        harness: "pi-native",
        skills: [],
      },
    ]);
  }

  it("promotes a previously-launched harness into the primary list", () => {
    localStorage.setItem("omnigent:recent-harnesses", JSON.stringify(["pi-native"]));
    mockClaudeAndPi();
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    // Pi isn't fully supported, but the user has launched it → inline, and with
    // nothing left to group the "More" trigger disappears entirely.
    expect(screen.getByTestId("new-chat-landing-agent-a_pi")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-harness-more")).toBeNull();
  });

  it("leaves an unused harness under 'More'", () => {
    // Control for the test above: same fixture, empty recents → Pi stays behind
    // "More", proving the promotion comes from the stored list.
    mockClaudeAndPi();
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.queryByTestId("new-chat-landing-agent-a_pi")).toBeNull();
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
    expect(screen.getByTestId("new-chat-landing-agent-a_pi")).toBeTruthy();
  });

  it("matches a stored reversed harness alias against its canonical spec", () => {
    // Older entries (and the server's reversed spelling) store "native-pi";
    // it must still promote the canonical pi-native row.
    localStorage.setItem("omnigent:recent-harnesses", JSON.stringify(["native-pi"]));
    mockClaudeAndPi();
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-agent-a_pi")).toBeTruthy();
  });

  it("ignores a malformed recent-harnesses entry", () => {
    // A corrupted value must not crash the picker — it falls back to the
    // support-level split.
    localStorage.setItem("omnigent:recent-harnesses", "{not json");
    mockClaudeAndPi();
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-agent-a_claude")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-agent-a_pi")).toBeNull();
  });

  it("keeps the hide-unconfigured preference ahead of a recent harness", () => {
    // Recency promotes within what can launch here; it must not resurrect a
    // harness the host can't run, which is the whole point of the preference.
    localStorage.setItem("omnigent:recent-harnesses", JSON.stringify(["pi-native"]));
    writeHideUnconfiguredHarnesses(true);
    mockClaudeAndPi();
    mockHosts([
      {
        ...host("online"),
        configured_harnesses: { "claude-native": true, "pi-native": false },
      } as Host,
    ]);
    renderLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.getByTestId("new-chat-landing-agent-a_claude")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-agent-a_pi")).toBeNull();
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
    const hostChip = screen.getByTestId("new-chat-landing-host-chip");
    expect(hostChip.textContent).toContain("No hosts");
    expect(hostChip.querySelector(".bg-success")).toBeNull();
    expect(hostChip.querySelector(".lucide-monitor")).not.toBeNull();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    // …and the connect item is still present, so a fresh user can unblock.
    expect(screen.getByTestId("new-chat-landing-connect-host")).toBeTruthy();
  });

  it("shows the Claude Code config knobs (model / effort / permission mode) in the gear modal", () => {
    renderLanding();
    // The knobs live in the gear-icon config modal — absent until it's opened.
    expect(screen.queryByTestId("new-chat-landing-config-model")).toBeNull();
    // a1 (Claude Code, claude-native) is the default agent. Open its config
    // modal: model + effort + permission-mode selects all appear together.
    openAgentConfig("a1");
    expect(screen.getByTestId("new-chat-landing-config-model")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-config-effort")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-config-permission")).toBeTruthy();
    // The model select offers the selected host's live catalog (mocked to
    // Opus 4.8 / Sonnet 4.6 / Haiku 4.5) — never the removed/static rows.
    openSelect("new-chat-landing-config-model");
    expect(screen.getByText("Opus 4.8")).toBeTruthy();
    expect(screen.getByText("Sonnet 4.6")).toBeTruthy();
    expect(screen.queryByText("Fable")).toBeNull();
    expect(screen.queryByText("Sonnet 5")).toBeNull();
    closeMenu();
    // The permission select offers every permission mode.
    openSelect("new-chat-landing-config-permission");
    expect(screen.getByText("Plan")).toBeTruthy();
    expect(screen.getByText("Bypass permissions")).toBeTruthy();
  });

  it("shows the Codex approval-mode knob in the gear modal", () => {
    renderLanding();
    // Open Codex's (a2) config modal — it carries the approval-mode select.
    openAgentConfig("a2");
    expect(screen.getByTestId("new-chat-landing-config-model")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-config-approval")).toBeTruthy();
    openSelect("new-chat-landing-config-approval");
    expect(screen.getByText("Full access")).toBeTruthy();
    expect(screen.getByText("Read only")).toBeTruthy();
  });

  it("sends the selected Codex launch model without changing Claude's remembered model", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();

    openAgentConfig("a2");
    openSelect("new-chat-landing-config-model");
    expect(screen.getAllByText("Default (databricks-gpt-5-5)").length).toBeGreaterThan(0);
    expect(screen.getByText("databricks-gpt-5-6")).toBeTruthy();
    fireEvent.click(screen.getByText("databricks-gpt-5-6"));
    saveConfig();

    // The Codex model is remembered under codex-native only; Claude Code's
    // picker should reopen on its own Default instead of inheriting the GPT id.
    openAgentConfig("a1");
    expect(screen.getByTestId("new-chat-landing-config-model").textContent).toContain("Default");
    expect(screen.getByTestId("new-chat-landing-config-model").textContent).not.toContain(
      "databricks-gpt-5-6",
    );
    saveConfig();

    openAgentConfig("a2");
    expect(screen.getByTestId("new-chat-landing-config-model").textContent).toContain(
      "databricks-gpt-5-6",
    );
    saveConfig();
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "run the build" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    const [, init] = authenticatedFetchMock.mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string) as Record<string, unknown>;
    expect(body.model_override).toBe("databricks-gpt-5-6");
    expect(body.reasoning_effort).toBeUndefined();
    expect(useHostModelOptionsMock).toHaveBeenCalledWith("host_1", "codex-native", true);
  });

  it("arms codex full bypass via the Approval dropdown and shows the warning banner", () => {
    renderLanding();
    // Open Codex's (a2) config modal; bypass is the most-permissive Approval option.
    openAgentConfig("a2");
    expect(screen.queryByTestId("new-chat-landing-bypass-sandbox-banner")).toBeNull();
    pickSelectOption("new-chat-landing-config-approval", "Bypass approvals & sandbox");
    // The trigger reflects the pick and the in-modal red danger banner appears.
    expect(screen.getByTestId("new-chat-landing-config-approval").textContent).toContain(
      "Bypass approvals & sandbox",
    );
    const banner = screen.getByTestId("new-chat-landing-bypass-sandbox-banner");
    expect(banner.textContent).toContain("approvals and the sandbox disabled");
  });

  it("disarms the dangerous bypass when the agent changes (re-arm per context)", () => {
    renderLanding();
    // Arm bypass on Codex (a2): open its config modal, pick Bypass, Save.
    openAgentConfig("a2");
    pickSelectOption("new-chat-landing-config-approval", "Bypass approvals & sandbox");
    saveConfig();
    // Armed → the persistent banner is up under the composer.
    expect(screen.getByTestId("new-chat-landing-bypass-sandbox-active-banner")).toBeTruthy();

    // Switch away to Claude (a1): the armed bypass must clear immediately, so
    // the persistent banner disappears (Claude has no bypass option at all).
    selectAgent("a1");
    expect(screen.queryByTestId("new-chat-landing-bypass-sandbox-active-banner")).toBeNull();

    // Switch back to Codex and reopen its config modal: Approval is back at
    // Default (no banner) — bypass must be re-armed for this fresh context.
    openAgentConfig("a2");
    expect(screen.getByTestId("new-chat-landing-config-approval").textContent).toContain("Default");
    expect(screen.queryByTestId("new-chat-landing-bypass-sandbox-banner")).toBeNull();
  });

  it("seeds the bypass-sandbox label in the create body when armed", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    openAgentConfig("a2");
    pickSelectOption("new-chat-landing-config-approval", "Bypass approvals & sandbox");
    // Save to commit, then submit a real task.
    saveConfig();
    // The persistent banner remains visible under the composer after the
    // config modal closes.
    expect(screen.getByTestId("new-chat-landing-bypass-sandbox-active-banner")).toBeTruthy();
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "run the build" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    const [, init] = authenticatedFetchMock.mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string) as Record<string, unknown>;
    const labels = body.labels as Record<string, string>;
    // The label is what the runner reads to launch with the bypass flag.
    expect(labels["omnigent.codex_native.bypass_sandbox"]).toBe("1");
    // The native wrapper labels still ride alongside it.
    expect(labels["omnigent.wrapper"]).toBe("codex-native-ui");
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

  it("keeps footer chip labels compact, muted and truncated", async () => {
    // Land with `?project=` so the branch chip (worktree) renders alongside the
    // others for the truncate-cap assertions.
    renderLanding({}, "/?project=docs");
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    // The host / working-directory / worktree chips each clamp their label to a
    // fixed max width and `truncate` it, so a long value (a deep working-directory
    // path, a long branch name) is ellipsized rather than growing the chip and
    // pushing the tray onto a second row. Dropping `truncate` or the `max-w-*`
    // cap would regress the single-row layout this guards.
    const label = (testid: string) => screen.getByTestId(testid).querySelector("span.truncate");

    for (const testid of [
      "new-chat-landing-workspace-chip",
      "new-chat-landing-host-chip",
      "new-chat-landing-branch-chip",
    ]) {
      expect(screen.getByTestId(testid)).toHaveClass("text-sm", "text-muted-foreground");
      expect(label(testid)).toHaveClass("text-sm");
      expect(label(testid)).not.toHaveClass("text-foreground");
    }
    expect(label("new-chat-landing-workspace-chip")?.className).toContain("max-w-40");
    expect(label("new-chat-landing-host-chip")?.className).toContain("max-w-32");
    expect(label("new-chat-landing-branch-chip")?.className).toContain("max-w-32");
    expect(
      screen.getByTestId("new-chat-landing-host-chip").querySelector(".bg-success"),
    ).not.toBeNull();
    expect(
      screen.getByTestId("new-chat-landing-host-chip").querySelector(".lucide-monitor"),
    ).toBeNull();
    expect(screen.getByTestId("new-chat-landing-branch-chip")).toHaveTextContent("Worktree");
  });

  it("opens the setup dialog and installs an installable harness from it", () => {
    // host_1 reports codex-native (agent a2) as not set up; the server accepts
    // that harness, so the composer notice offers a "Set up Codex" action that
    // opens the dialog, and the dialog's Install button fires the install.
    const installMutate = vi.fn();
    vi.mocked(useInstallHarness).mockReturnValue({
      mutate: installMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useInstallHarness>);
    mockHosts([{ ...host("online"), configured_harnesses: { "codex-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");

    // The composer action is labelled with the agent, not "the harness".
    const setup = screen.getByTestId("new-chat-landing-harness-setup");
    expect(setup.textContent).toBe("Set up Codex");
    fireEvent.click(setup);
    // Dialog opens with a one-click Install (codex-native is installable).
    fireEvent.click(screen.getByTestId("harness-setup-install"));
    expect(installMutate).toHaveBeenCalledWith("codex-native", expect.anything());
  });

  it("gates Set up auth until the harness is installed (no auth-before-install)", () => {
    // codex-native not installed: the credential form must NOT be available
    // yet — the auth row shows a DISABLED "Set up auth" (install first).
    // Clicking it does nothing / the form doesn't expand, so an auth-first write
    // (which would leave the dot red) can't happen.
    mockHosts([{ ...host("online"), configured_harnesses: { "codex-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));

    // Install is offered; the auth row's control is present but disabled.
    expect(screen.getByTestId("harness-setup-install")).toBeTruthy();
    const setUpAuth = screen.getByTestId("harness-setup-add-credential") as HTMLButtonElement;
    expect(setUpAuth.textContent).toBe("Set up auth");
    expect(setUpAuth.disabled).toBe(true);
    fireEvent.click(setUpAuth);
    expect(screen.queryByTestId("harness-credential-form")).toBeNull();
  });

  it("enables Set up auth once the harness is installed (needs-auth)", () => {
    // Installed but no credential → the auth row's "Set up auth" is enabled and
    // expands the credential form.
    mockHosts([
      { ...host("online"), configured_harnesses: { "codex-native": "needs-auth" } } as Host,
    ]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));

    const setUpAuth = screen.getByTestId("harness-setup-add-credential") as HTMLButtonElement;
    expect(setUpAuth.textContent).toBe("Set up auth");
    expect(setUpAuth.disabled).toBe(false);
    fireEvent.click(setUpAuth);
    expect(screen.getByTestId("harness-credential-form")).toBeTruthy();
  });

  it("marks its Install button loading while THIS harness's install is pending", () => {
    // The dialog derives the in-flight indicator from React Query's mutation
    // state via useInstallingHarnesses (observer-independent, so a concurrent
    // install can't strand it). When that reports codex-native pending, the
    // button shows the loading/disabled state.
    vi.mocked(useInstallingHarnesses).mockReturnValue(new Set(["codex-native"]));
    mockHosts([{ ...host("online"), configured_harnesses: { "codex-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");

    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));
    expect((screen.getByTestId("harness-setup-install") as HTMLButtonElement).disabled).toBe(true);
    // The "Installing…" caption sits INSIDE the install step row (next to what
    // it describes), not stranded at the dialog footer.
    const installStep = screen.getByTestId("harness-setup-step-install");
    const installingCaption = screen.getByTestId("harness-setup-installing");
    expect(installStep.contains(installingCaption)).toBe(true);
  });

  it("offers the inline credential form (not an install) for codex needs-auth", async () => {
    // Binary present, just not authed → the auth step offers "Set up auth",
    // which expands the inline credential form. Never an Install button (a
    // reinstall wouldn't add the credential). The subscription `codex login` is
    // one option inside the form (the UI can't drive browser OAuth).
    copyTextMock.mockClear();
    mockHosts([
      { ...host("online"), configured_harnesses: { "codex-native": "needs-auth" } } as Host,
    ]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");

    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));
    expect(screen.queryByTestId("harness-setup-install")).toBeNull();
    // Open the inline form.
    fireEvent.click(screen.getByTestId("harness-setup-add-credential"));
    expect(screen.getByTestId("harness-credential-form")).toBeTruthy();
    expect(screen.getByTestId("harness-credential-key")).toBeTruthy();
    // The subscription login is a copy signpost inside the form.
    const loginCopy = screen.getByTestId("harness-credential-login-copy");
    expect(loginCopy.textContent).toContain("codex login");
    fireEvent.click(loginCopy);
    await waitFor(() => expect(copyTextMock).toHaveBeenCalledWith("codex login"));
  });

  it("hides the Install button when the server doesn't list the harness as installable", () => {
    // Defence in depth: the server published an install step for codex-native,
    // but the harness is NOT in installable_harnesses (a catalog/allowlist
    // drift). The Install button must not render — offering it would drive a
    // POST the install route rejects. The step falls back to no control (an
    // install step carries no copyable command).
    mockHosts([{ ...host("online"), configured_harnesses: { "codex-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: [], // step catalog says install; allowlist disagrees
    });
    selectUnconfiguredAgent("a2");

    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));
    expect(screen.queryByTestId("harness-setup-install")).toBeNull();
  });

  it("toast says 'ready' only when the install returns a launchable harness", () => {
    // The install POST returns the refreshed readiness. codex-native comes back
    // true (ready) → the success toast says ready.
    showToastMock.mockClear();
    vi.mocked(useInstallHarness).mockReturnValue({
      mutate: (harness: string, opts?: { onSuccess?: (r: unknown) => void }) =>
        opts?.onSuccess?.({
          object: "harness_install",
          harness,
          configured_harnesses: { "codex-native": true },
        }),
      isPending: false,
    } as unknown as ReturnType<typeof useInstallHarness>);
    mockHosts([{ ...host("online"), configured_harnesses: { "codex-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");

    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));
    fireEvent.click(screen.getByTestId("harness-setup-install"));
    expect(showToastMock).toHaveBeenCalledWith(expect.stringContaining("is ready on"));
  });

  it("toast says 'one more step' when the install leaves the harness needing auth", () => {
    // codex installs but still needs a login → readiness comes back
    // "needs-auth", not true. The toast must not claim "ready" (which would
    // contradict the sign-in row the checklist still shows).
    showToastMock.mockClear();
    vi.mocked(useInstallHarness).mockReturnValue({
      mutate: (harness: string, opts?: { onSuccess?: (r: unknown) => void }) =>
        opts?.onSuccess?.({
          object: "harness_install",
          harness,
          configured_harnesses: { "codex-native": "needs-auth" },
        }),
      isPending: false,
    } as unknown as ReturnType<typeof useInstallHarness>);
    mockHosts([{ ...host("online"), configured_harnesses: { "codex-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["codex", "codex-native"],
    });
    selectUnconfiguredAgent("a2");

    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));
    fireEvent.click(screen.getByTestId("harness-setup-install"));
    expect(showToastMock).toHaveBeenCalledWith(expect.stringContaining("one more step"));
    expect(showToastMock).not.toHaveBeenCalledWith(expect.stringContaining("is ready"));
  });

  it("shows a fallback message when the server published no steps for the spelling", () => {
    // Feature on, harness unconfigured, but no setup_steps for this spelling
    // (a1 = claude-native, which the stubbed useHarnessSetupSteps doesn't
    // cover). The dialog must not be an empty dead-end — it points at the CLI.
    mockHosts([{ ...host("online"), configured_harnesses: { "claude-native": false } } as Host]);
    renderLanding({
      harness_install_enabled: true,
      installable_harnesses: ["claude", "claude-native"],
    });
    selectAgent("a1");

    fireEvent.click(screen.getByTestId("new-chat-landing-harness-setup"));
    expect(screen.getByTestId("harness-setup-empty").textContent).toContain("omni setup");
    expect(screen.queryByTestId("harness-setup-install")).toBeNull();
  });

  it("falls back to the original setup guidance when the feature is off", () => {
    // Flag OFF (renderLanding default) → the pre-feature UI: the warning shows
    // the descriptive "run omni setup" message, NOT the "Set up" action or
    // dialog. This is the no-op-when-disabled contract.
    mockHosts([
      { ...host("online"), configured_harnesses: { "codex-native": "needs-auth" } } as Host,
    ]);
    renderLanding();
    selectUnconfiguredAgent("a2");

    const warning = screen.getByTestId("new-chat-landing-harness-warning");
    expect(warning.textContent).toContain("codex login");
    // No "Set up" affordance and no dialog trigger when the feature is off.
    expect(screen.queryByTestId("new-chat-landing-harness-setup")).toBeNull();
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

  it("lists existing worktrees and starts directly in a selected one (git bind mode)", async () => {
    // The seeded repo has one linked worktree; the main tree is filtered out.
    useHostWorktreesMock.mockReturnValue({
      data: [
        { path: "/Users/corey/repo", branch: "main", is_main: true, detached: false },
        {
          path: "/Users/corey/repo-worktrees/feature-x",
          branch: "feature/x",
          is_main: false,
          detached: false,
        },
      ],
    } as unknown as ReturnType<typeof useHostWorktrees>);
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    // Open the worktree popover, focus the branch combobox to reveal the
    // existing-worktree dropdown, and select the one linked worktree.
    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    fireEvent.focus(screen.getByTestId("new-chat-landing-branch-input"));
    const options = screen.getAllByTestId("new-chat-landing-worktree-option");
    expect(options).toHaveLength(1); // main tree excluded
    expect(options[0].textContent).toContain("feature/x");
    // onMouseDown (fires before the input's blur) drives selection.
    fireEvent.mouseDown(options[0]);

    // Selecting a worktree auto-closes the popover.
    await waitFor(() => expect(screen.queryByTestId("new-chat-landing-branch-input")).toBeNull());

    // Reopen the chip: the warning shows and the branch field is prefilled with
    // the selected worktree's branch.
    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    await screen.findByTestId("new-chat-landing-existing-worktree-warning");
    expect((screen.getByTestId("new-chat-landing-branch-input") as HTMLInputElement).value).toBe(
      "feature/x",
    );

    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "work in the worktree" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));

    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    const [, init] = authenticatedFetchMock.mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string) as {
      workspace?: string;
      git?: { branch_name: string; existing_worktree?: boolean; base_branch?: string };
    };
    // Workspace is bound straight to the worktree dir. The git block is in
    // bind mode (`existing_worktree`): no worktree is created, but the
    // worktree's branch rides along as `branch_name` so the sidebar shows it
    // and the delete flow can offer to remove it. No base_branch on a bind.
    expect(body.workspace).toBe("/Users/corey/repo-worktrees/feature-x");
    expect(body.git?.existing_worktree).toBe(true);
    expect(body.git?.branch_name).toBe("feature/x");
    expect(body.git?.base_branch).toBeUndefined();
  });

  it("creates a new worktree when the prefilled branch name is edited", async () => {
    useHostWorktreesMock.mockReturnValue({
      data: [
        {
          path: "/Users/corey/repo-worktrees/feature-x",
          branch: "feature/x",
          is_main: false,
          detached: false,
        },
      ],
    } as unknown as ReturnType<typeof useHostWorktrees>);
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    fireEvent.focus(screen.getByTestId("new-chat-landing-branch-input"));
    fireEvent.mouseDown(screen.getByTestId("new-chat-landing-worktree-option"));
    // Selection auto-closes the popover — reopen to edit the prefilled branch.
    await waitFor(() => expect(screen.queryByTestId("new-chat-landing-branch-input")).toBeNull());
    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    await screen.findByTestId("new-chat-landing-existing-worktree-warning");

    // Edit the branch away from the prefill: now it's a NEW worktree request.
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/y" },
    });
    // Warning gone once the name diverges from the existing worktree's branch.
    expect(screen.queryByTestId("new-chat-landing-existing-worktree-warning")).toBeNull();

    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "branch off" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));

    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    const [, init] = authenticatedFetchMock.mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string) as {
      git?: { branch_name: string; existing_worktree?: boolean };
    };
    // A new worktree for the edited branch name is requested — this is a
    // create, not a bind, so `existing_worktree` is not set.
    expect(body.git?.branch_name).toBe("feature/y");
    expect(body.git?.existing_worktree).toBeUndefined();
  });

  it("filters the worktree dropdown as you type in the branch combobox", async () => {
    useHostWorktreesMock.mockReturnValue({
      data: [
        {
          path: "/Users/corey/repo-worktrees/feature-x",
          branch: "feature/x",
          is_main: false,
          detached: false,
        },
        {
          path: "/Users/corey/repo-worktrees/bugfix-login",
          branch: "bugfix/login",
          is_main: false,
          detached: false,
        },
      ],
    } as unknown as ReturnType<typeof useHostWorktrees>);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    // Radix autofocuses the branch combobox on open, so the dropdown of both
    // worktrees shows immediately (a focus event keeps it open in jsdom too).
    fireEvent.focus(screen.getByTestId("new-chat-landing-branch-input"));
    expect(screen.getAllByTestId("new-chat-landing-worktree-option")).toHaveLength(2);

    // Typing in the branch field narrows to matching branch/path substrings.
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "bugfix" },
    });
    const options = screen.getAllByTestId("new-chat-landing-worktree-option");
    expect(options).toHaveLength(1);
    expect(options[0].textContent).toContain("bugfix/login");

    // A name matching nothing hides the dropdown entirely — that name becomes
    // a NEW worktree on submit rather than selecting an existing one.
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "brand-new-branch" },
    });
    expect(screen.queryByTestId("new-chat-landing-worktree-dropdown")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-worktree-option")).toBeNull();
  });

  it("generates a unique worktree branch name and sends it on create", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
    // Clicking the generate button fills a "worktree-<hex>" name.
    fireEvent.mouseDown(screen.getByTestId("new-chat-landing-branch-generate"));
    const branchInput = screen.getByTestId("new-chat-landing-branch-input") as HTMLInputElement;
    expect(branchInput.value).toMatch(/^worktree-[0-9a-f]{8}$/);

    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "spin up a scratch worktree" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));

    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(1));
    const [, init] = authenticatedFetchMock.mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string) as {
      git?: { branch_name: string };
    };
    // The generated name rides through as a new-worktree create.
    expect(body.git?.branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
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
    expect(screen.getByTestId("new-chat-landing-repo-chip")).toHaveTextContent("Repository");
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
      .getAllByText("This machine")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    expect(hostItem).toBeTruthy();
    expect(
      sandboxOption.compareDocumentPosition(hostItem!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Picking the host restores the workspace flow (file-browser chip,
    // worktree chip) — the sandbox default doesn't wedge the normal path.
    fireEvent.click(hostItem!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain(
        "This machine",
      ),
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

  it("shows the default hero heading in the normal new-session flow (no project)", async () => {
    // Without a `?project=` param the session is unfiled — the hero keeps its
    // generic prompt, and there is no project chip in the tray.
    renderLanding();

    await screen.findByTestId("new-chat-landing-input");
    expect(screen.getByText("What should we build?")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-project-chip")).toBeNull();
  });

  it("files a pre-selected project, and invalidates project sessions", async () => {
    // Sequence: create POST → list projects (resolve name→id) → PATCH project_id.
    authenticatedFetchMock
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: "conv_new" }) } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ object: "list", data: [{ id: "p_docs", name: "docs" }] }),
      } as Response)
      .mockResolvedValue({ ok: true, json: async () => ({ id: "conv_new" }) } as Response);
    const invalidateSpy = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    // A `?project=` landing (e.g. via the sidebar's per-project pencil) names the
    // project in the hero heading rather than a tray chip.
    renderLanding({}, "/?project=docs");

    await waitFor(() => expect(screen.getByText("docs")).toBeTruthy());

    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "write the docs" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));

    // Create POST, then the name→id resolution, then a PATCH that files the
    // freshly-created session via first-class project_id.
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(3));
    expect(authenticatedFetchMock.mock.calls[0][0]).toBe("/v1/sessions");
    // Born filed: the create POST stamps the project's `omni_project` label so
    // the session groups under its project from its first sidebar appearance
    // (the sidebar dual-reads the label OR project_id), rather than flashing
    // through the ungrouped "Sessions" section until the follow-up move's
    // project_id write catches up in the search-indexed session list.
    const createBody = JSON.parse(
      (authenticatedFetchMock.mock.calls[0][1] as RequestInit).body as string,
    ) as { labels?: Record<string, string> };
    expect(createBody.labels?.omni_project).toBe("docs");
    expect(authenticatedFetchMock.mock.calls[1][0]).toBe("/v1/projects");
    const [patchUrl, patchInit] = authenticatedFetchMock.mock.calls[2];
    expect(patchUrl).toBe("/v1/sessions/conv_new");
    expect((patchInit as RequestInit).method).toBe("PATCH");
    const patchBody = JSON.parse((patchInit as RequestInit).body as string) as {
      project_id: string;
    };
    expect(patchBody.project_id).toBe("p_docs");

    // The target folder fetches its own paginated list (useProjectSessions),
    // so filing the new session must invalidate it — otherwise the row only
    // appears after a manual refresh.
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["project-sessions"] }),
    );
    invalidateSpy.mockRestore();
  });

  it("names the project in the hero heading from the ?project= query param", async () => {
    // The sidebar's per-project "new session" pencil lands here with the
    // project pre-selected — the hero heading reflects it with no interaction.
    authenticatedFetchMock
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: "conv_new" }) } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ object: "list", data: [{ id: "p_sprint", name: "Sprint 42" }] }),
      } as Response)
      .mockResolvedValue({ ok: true, json: async () => ({ id: "conv_new" }) } as Response);
    renderLanding({}, "/?project=Sprint%2042");

    await waitFor(() => expect(screen.getByText("Sprint 42")).toBeTruthy());

    // Creating a session files it under that pre-filled project.
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "kick off the sprint" },
    });
    fireEvent.submit(screen.getByTestId("new-chat-landing-composer"));

    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalledTimes(3));
    const [patchUrl, patchInit] = authenticatedFetchMock.mock.calls[2];
    expect(patchUrl).toBe("/v1/sessions/conv_new");
    const patchBody = JSON.parse((patchInit as RequestInit).body as string) as {
      project_id: string;
    };
    expect(patchBody.project_id).toBe("p_sprint");
  });

  it("clamps a long project name in the hero heading so it can't overflow the row", async () => {
    // Project names are capped at 100 chars server-side, which at text-3xl is
    // far wider than the centered container. The heading must wrap + clamp
    // rather than render at full width on one line (jsdom can't lay out, so we
    // assert the class contract that guards this — dropping any of these would
    // regress to the overflow the old max-w-32 chip prevented).
    const longName = "A".repeat(100);
    renderLanding({}, `/?project=${encodeURIComponent(longName)}`);

    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading.textContent).toBe(longName);
    // min-w-0 lets the heading shrink inside the flex row; line-clamp-2 +
    // break-words wrap/ellipsize instead of overflowing.
    expect(heading.className).toContain("min-w-0");
    expect(heading.className).toContain("line-clamp-2");
    expect(heading.className).toContain("break-words");
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

  it("filters by the typed query (substring) and fills the draft on click", () => {
    mockAgents([skilledAgent()]);
    renderLanding();
    // Substring match: both skills contain "rev" (review-pr, cross-review).
    typeMessage("/rev");
    expect(screen.getByTestId("slash-menu-item-review-pr")).toBeTruthy();
    expect(screen.getByTestId("slash-menu-item-cross-review")).toBeTruthy();
    // A more specific substring narrows to one.
    typeMessage("/pr");
    expect(screen.getByTestId("slash-menu-item-review-pr")).toBeTruthy();
    expect(screen.queryByTestId("slash-menu-item-cross-review")).toBeNull();
    fireEvent.click(screen.getByTestId("slash-menu-item-review-pr"));
    // Selection fills "/name " (trailing space, caret ready for args).
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

  it("Tab completes a match found only mid-name (exercises slashMenuMatches, not just the render filter)", () => {
    mockAgents([skilledAgent()]);
    renderLanding();
    // "pr" is a substring of "review-pr" but a prefix of no command. Tab
    // completion reads slashMenuMatches/slashMenuIndex, so this only
    // completes if the keyboard-nav filter is substring-based — guarding it
    // from reverting to prefix matching and diverging from the rendered list.
    typeMessage("/pr");
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-input"), { key: "Tab" });
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

// The "@"-file-mention browser on the launcher mirrors the in-session
// composer, but its file source is the *host filesystem* (no session/runner
// exists yet) and its paths are converted from the host's absolute form to
// workspace-relative for the chip and the "[Attached: …]" marker.
describe("NewChatLandingScreen @-file-mention", () => {
  const ROOT = "/Users/corey/repo";
  function dir(path: string): HostFilesystemEntry {
    return {
      name: path.split("/").pop() ?? "",
      path,
      type: "directory",
      bytes: null,
      modified_at: 0,
    };
  }
  function file(path: string): HostFilesystemEntry {
    return { name: path.split("/").pop() ?? "", path, type: "file", bytes: 10, modified_at: 0 };
  }
  // Path-aware listing: the workspace root holds an "omnigent" folder + a
  // README; drilling into "omnigent" reveals a nested folder + a file. Keyed by
  // the absolute path so drill-down and relative-path mapping are exercised for
  // real (a fixed stub couldn't distinguish the two levels).
  function mockFsByPath() {
    useHostFilesystemMock.mockImplementation(((_hostId: string | null, path: string | null) => {
      let entries: HostFilesystemEntry[] = [];
      if (path === ROOT) entries = [dir(`${ROOT}/omnigent`), file(`${ROOT}/README.md`)];
      else if (path === `${ROOT}/omnigent`)
        entries = [dir(`${ROOT}/omnigent/inner`), file(`${ROOT}/omnigent/cli.py`)];
      return {
        data: { entries, truncated: false },
        isLoading: false,
        error: null,
        isPlaceholderData: false,
      };
    }) as unknown as typeof useHostFilesystem);
  }

  beforeEach(() => {
    setupLandingMocks();
    setPendingInitialPromptMock.mockReset();
    mockFsByPath();
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  function input() {
    return screen.getByTestId("new-chat-landing-input");
  }

  it("opens the menu listing workspace files when '@' is typed (native agent)", async () => {
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    fireEvent.change(input(), { target: { value: "@", selectionStart: 1 } });
    // Host absolute paths are shown as workspace-relative rows (folders first).
    expect(screen.getByTitle("Open omnigent")).toBeInTheDocument();
    expect(screen.getByTitle("Attach README.md")).toBeInTheDocument();
  });

  it("does NOT open the menu for a non-native (SDK) agent", () => {
    // Gate parity with the in-session composer: mentions are native-only.
    mockAgents([
      {
        id: "sdk1",
        name: "my-sdk-agent",
        display_name: "SDK Agent",
        description: null,
        harness: "claude-sdk",
        skills: [],
      },
    ]);
    renderLanding();
    fireEvent.change(input(), { target: { value: "@", selectionStart: 1 } });
    expect(screen.queryByTitle("Open omnigent")).not.toBeInTheDocument();
  });

  it("drills into a folder and delivers the chosen file as a workspace-relative marker", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    fireEvent.change(input(), { target: { value: "@", selectionStart: 1 } });
    // Nested files are hidden until the folder is opened (drill-down).
    expect(screen.queryByTitle("Attach cli.py")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Open omnigent"));
    fireEvent.click(screen.getByTitle("Attach cli.py"));
    // The chip shows the workspace-relative path, not the host-absolute one.
    expect(screen.getByText("@omnigent/cli.py")).toBeInTheDocument();

    fireEvent.change(input(), { target: { value: "explain this", selectionStart: 12 } });
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The contract: the first message carries "[Attached: <relpath>]" so the
    // runner (rooted at the workspace) reads the on-disk file — relative, never
    // the "/Users/corey/repo/…" absolute path the host filesystem returned.
    await waitFor(() => expect(setPendingInitialPromptMock).toHaveBeenCalled());
    const [, payload] = setPendingInitialPromptMock.mock.calls[0]!;
    expect((payload as { text: string }).text).toBe("[Attached: omnigent/cli.py]\n\nexplain this");
  });

  it("suppresses stale parent rows while a drilled directory is still loading", async () => {
    // ``useHostFilesystem`` keeps the previous directory's rows on screen as
    // placeholder data while the next fetch is in flight (keepPreviousData):
    // ``isLoading`` is false, only ``isPlaceholderData`` is true. If the menu
    // rendered that placeholder it would show the *parent's* files as though
    // they lived inside the drilled child, and a click/Enter would attach the
    // wrong entry. The menu must collapse to "Loading…" until the child's own
    // listing arrives.
    const rootEntries = [dir(`${ROOT}/omnigent`), file(`${ROOT}/README.md`)];
    useHostFilesystemMock.mockImplementation(((_hostId: string | null, path: string | null) => {
      // Root resolves normally; the drilled path is still serving the parent's
      // rows as placeholder data (the mid-fetch window we're regression-testing).
      const isPlaceholderData = path !== ROOT;
      return {
        data: { entries: rootEntries, truncated: false },
        isLoading: false,
        error: null,
        isPlaceholderData,
      };
    }) as unknown as typeof useHostFilesystem);

    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    fireEvent.change(input(), { target: { value: "@", selectionStart: 1 } });
    // Root listing renders its rows.
    expect(screen.getByTitle("Open omnigent")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Open omnigent"));

    // Drilled-but-loading: the loading row shows and the parent's stale rows are
    // gone (without the isPlaceholderData guard they'd appear as the child's).
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByTitle("Attach README.md")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Open omnigent")).not.toBeInTheDocument();
  });

  it("attaches a whole folder with a trailing-slash marker", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );

    fireEvent.change(input(), { target: { value: "@", selectionStart: 1 } });
    // The folder row's "+" button attaches the directory as a unit.
    fireEvent.click(screen.getByLabelText("Attach whole folder omnigent"));
    expect(screen.getByText("@omnigent/")).toBeInTheDocument();

    fireEvent.change(input(), { target: { value: "review it", selectionStart: 9 } });
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(setPendingInitialPromptMock).toHaveBeenCalled());
    const [, payload] = setPendingInitialPromptMock.mock.calls[0]!;
    expect((payload as { text: string }).text).toBe("[Attached: omnigent/]\n\nreview it");
  });

  it("removes a tagged chip when its ✕ is clicked", () => {
    renderLanding();
    fireEvent.change(input(), { target: { value: "@", selectionStart: 1 } });
    fireEvent.click(screen.getByTitle("Attach README.md"));
    expect(screen.getByText("@README.md")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Remove README.md"));
    expect(screen.queryByText("@README.md")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Agent picker + config gear
//
// The picker dropdown only SELECTS the agent/harness now; run-config knobs
// live in the gear-icon config modal beside it (viewport-independent — there's
// no separate mobile drill-in path). Tapping a row commits the pick and closes
// the menu; the gear then opens the selected agent's config modal.
// ---------------------------------------------------------------------------

describe("NewChatLandingScreen agent picker + config gear", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  /** Open the picker (Radix opens on pointerdown). */
  function openPicker(): void {
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  }

  it("selects an agent by clicking its row (no inline knobs in the dropdown)", () => {
    renderLanding();
    openPicker();
    // The dropdown lists agents only — no config knobs live inside it.
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-config-approval")).toBeNull();
    // Clicking a2 (Codex) commits the pick — the trigger reflects it.
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-a2"));
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Codex");
  });

  it("opens the selected agent's config modal from the gear icon", () => {
    renderLanding();
    // a1 (Claude Code) is selected by default; the gear opens its config modal.
    expect(screen.queryByTestId("new-chat-landing-config-modal")).toBeNull();
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-modal")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-config-permission")).toBeTruthy();
  });

  it("summarizes the current settings in the gear tooltip on hover", async () => {
    renderLanding();
    // Set a non-default permission mode via the modal so the tooltip has
    // something specific to show, then Save.
    openAgentConfig("a1");
    pickSelectOption("new-chat-landing-config-permission", "Plan");
    saveConfig();
    // Focusing the gear reveals its tooltip (Radix opens on focus without the
    // hover delay) listing the live settings — the Claude Code knobs
    // (Model / Effort / Permissions), reflecting the pick.
    fireEvent.focus(screen.getByTestId("new-chat-landing-config-gear"));
    // Radix mounts the tooltip content (plus a visually-hidden a11y copy), so
    // assert on presence rather than a single node.
    await waitFor(() =>
      expect(screen.getAllByTestId("new-chat-landing-config-gear-tooltip").length).toBeGreaterThan(
        0,
      ),
    );
    const tooltip = screen.getAllByTestId("new-chat-landing-config-gear-tooltip")[0];
    expect(tooltip.textContent).toContain("Permissions:");
    expect(tooltip.textContent).toContain("Plan");
    expect(tooltip.textContent).toContain("Model:");
    // Unset effort reads "Default" (mirrors the modal), never the "—" sentinel.
    expect(tooltip.textContent).toContain("Effort: Default");
    expect(tooltip.textContent).not.toContain("—");
  });

  it("reflects an armed Codex bypass as the Approval value in the gear tooltip", async () => {
    renderLanding();
    // Arm bypass on Codex (a2) via the Approval dropdown, Save.
    openAgentConfig("a2");
    pickSelectOption("new-chat-landing-config-approval", "Bypass approvals & sandbox");
    saveConfig();
    fireEvent.focus(screen.getByTestId("new-chat-landing-config-gear"));
    await waitFor(() =>
      expect(screen.getAllByTestId("new-chat-landing-config-gear-tooltip").length).toBeGreaterThan(
        0,
      ),
    );
    const tooltip = screen.getAllByTestId("new-chat-landing-config-gear-tooltip")[0];
    // Bypass is the effective Approval value (mirrors the modal's single
    // control) — not a separate "Bypass: On" row alongside a stale preset.
    expect(tooltip.textContent).toContain("Approval: Bypass approvals & sandbox");
    expect(tooltip.textContent).not.toContain("Bypass: On");
  });

  it("shows the permission mode description in the dropdown footer, tracking hover", () => {
    renderLanding();
    openAgentConfig("a1");
    openSelect("new-chat-landing-config-permission");
    // The footer starts on the selected (Default) mode's blurb...
    const detail = screen.getByTestId("new-chat-landing-config-permission-detail");
    expect(detail.textContent).toContain("Prompts before edits and commands");
    // ...then follows the hovered option.
    fireEvent.pointerEnter(screen.getByRole("option", { name: "Plan" }));
    expect(detail.textContent).toContain("Plans only; makes no edits");
  });

  it("discards config changes on Cancel", () => {
    renderLanding();
    openAgentConfig("a1");
    // Change the permission mode to Plan, then Cancel — nothing commits, and
    // reopening shows the default again (the trigger reflects the value).
    pickSelectOption("new-chat-landing-config-permission", "Plan");
    expect(screen.getByTestId("new-chat-landing-config-permission").textContent).toContain("Plan");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-cancel"));
    expect(screen.queryByTestId("new-chat-landing-config-modal")).toBeNull();
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    // Reopened: Plan was discarded, the permission select is back at Default.
    expect(screen.getByTestId("new-chat-landing-config-permission").textContent).toContain(
      "Default",
    );
  });

  it("keeps the gear visible for routing-eligible agents even with no other knob (guard)", () => {
    // Defensive: if a routable harness ever has no permission/approval/cursor
    // knob and isn't a brain agent, Smart Routing (modal-only) still needs the
    // gear. Simulate by extending the routable set is not possible here, so we
    // assert the inverse contract: a NON-routable knob-less native agent hides
    // the gear (so the routing-eligible branch is what keeps it shown).
    mockAgents([
      {
        id: "a_bare",
        name: "opencode-native-ui",
        display_name: "OpenCode",
        description: null,
        harness: "opencode-native",
        skills: [],
      },
    ]);
    renderLanding({ smart_routing_enabled: true });
    // opencode-native has no knobs and isn't routable → no gear.
    expect(screen.queryByTestId("new-chat-landing-config-gear")).toBeNull();
  });
});

describe("NewChatLandingScreen custom-agent sandbox gating", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  // Select the managed sandbox as the target. The default mocks give one
  // online host (auto-selected), so we open the host chip and pick the
  // sandbox option pinned at the top.
  async function selectSandbox(): Promise<void> {
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("Sandbox"),
    );
  }

  it("hides 'Create custom agent' on a sandbox", async () => {
    renderLanding({ managed_sandboxes_enabled: true });
    await selectSandbox();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    // The item is omitted entirely on a sandbox target.
    expect(screen.queryByTestId("new-chat-landing-create-agent")).toBeNull();
  });

  it("shows 'Create custom agent' on a host and opens the dialog", async () => {
    renderLanding({ managed_sandboxes_enabled: true });
    // The managed default is the sandbox even with a host present, so switch
    // to the connected host (machine-1) first.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("Sandbox"),
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    const hostItem = screen
      .getAllByText("This machine")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    expect(hostItem).toBeTruthy();
    fireEvent.click(hostItem!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain(
        "This machine",
      ),
    );
    // With no custom agents yet, the create item is a top-level row (no
    // "Custom agents" submenu to hide it behind) and opens the dialog.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    // No custom agents → no "Custom agents" submenu; create must be top-level.
    expect(screen.queryByTestId("new-chat-landing-custom-agents")).toBeNull();
    const createItem = screen.getByTestId("new-chat-landing-create-agent");
    fireEvent.click(createItem);
    await waitFor(() => expect(screen.getByTestId("create-agent-dialog")).toBeTruthy());
  });

  // Switch the target to the connected host, then create + submit a pending
  // custom agent from the dialog so it becomes the selected agent.
  async function createAndSelectPendingAgentOnHost(): Promise<void> {
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("Sandbox"),
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    const hostItem = screen
      .getAllByText("This machine")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    fireEvent.click(hostItem!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain(
        "This machine",
      ),
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-create-agent"));
    await waitFor(() => expect(screen.getByTestId("create-agent-dialog")).toBeTruthy());
    fireEvent.change(screen.getByTestId("create-agent-name"), { target: { value: "my-agent" } });
    fireEvent.change(screen.getByTestId("create-agent-model"), {
      target: { value: "claude-sonnet-4-20250514" },
    });
    fireEvent.click(screen.getByTestId("create-agent-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("my-agent"),
    );
  }

  it("drops a selected pending custom agent when the target switches to a sandbox", async () => {
    renderLanding({ managed_sandboxes_enabled: true });
    await createAndSelectPendingAgentOnHost();
    // Switch back to the sandbox: the pending pick can't run there, so the
    // selection falls back to a real agent and the pending row disappears.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("Sandbox"),
    );
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain(
      "my-agent",
    );
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    expect(screen.queryByTestId("new-chat-landing-agent-pending")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Mobile drill-in navigation
//
// Touch devices can't hover, so the desktop submenu flyouts ("More" for
// needs-setup harnesses, "Custom agents") are unreachable there. Below the
// `md` breakpoint the picker swaps its contents in place: tapping the row
// drills into that group's page with a Back row. jsdom's matchMedia mock
// reports non-mobile, so these tests force the `max-width` query to match.
// ---------------------------------------------------------------------------

function forceMobileViewport(): () => void {
  const real = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: /max-width/.test(query),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return () => {
    window.matchMedia = real;
  };
}

describe("NewChatLandingScreen agent picker (mobile drill-in)", () => {
  let restoreViewport: () => void;
  beforeEach(() => {
    setupLandingMocks();
    restoreViewport = forceMobileViewport();
  });
  afterEach(() => {
    restoreViewport();
    cleanup();
    localStorage.clear();
  });

  function openPicker(): void {
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  }

  it("drills into the Custom agents page in place and returns via Back", () => {
    // A custom (non-builtin) agent lands in the Custom agents group.
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
        id: "ag_custom",
        name: "my-custom-agent",
        display_name: "My Custom Agent",
        description: null,
        harness: "claude-sdk",
        skills: [],
      },
    ]);
    renderLanding();
    openPicker();
    // The custom agent isn't inline — it's behind the "Custom agents" row.
    expect(screen.queryByTestId("new-chat-landing-agent-ag_custom")).toBeNull();
    // Tapping drills into the page in place (Claude Code inline row is gone).
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    expect(screen.getByTestId("new-chat-landing-agent-ag_custom")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-agent-a1")).toBeNull();
    // Back returns to the main list.
    fireEvent.click(screen.getByTestId("new-chat-landing-page-back"));
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-agent-ag_custom")).toBeNull();
  });

  it("drills into the More page for harnesses outside the supported set", () => {
    // cursor-native isn't fully supported → folded into "More" (Claude Code and
    // Codex lead inline), so touch gets a drill-in page with a Back row.
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
        id: "a_cursor",
        name: "cursor-native-ui",
        display_name: "Cursor",
        description: null,
        harness: "cursor-native",
        skills: [],
      },
    ]);
    renderLanding();
    openPicker();
    expect(screen.queryByTestId("new-chat-landing-agent-a_cursor")).toBeNull();
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-more"));
    expect(screen.getByTestId("new-chat-landing-agent-a_cursor")).toBeTruthy();
    fireEvent.click(screen.getByTestId("new-chat-landing-page-back"));
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Smart Routing (per-harness Model option) + the fully-auto Auto harness
//
// Two distinct products of the router, deliberately separate in the UI:
//  - "Smart Routing" is a Model choice on the two native harnesses whose
//    running CLI takes a per-turn model switch (claude-native / codex-native).
//    It sends cost_control_mode_override: "on" with NO model_override.
//  - The Auto harness is a harness choice on bundle agents: the router picks
//    harness AND model, so its config modal has nothing but Permissions.
// ---------------------------------------------------------------------------

describe("NewChatLandingScreen smart routing", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  // The Model row's option set per harness. Claude Code lists its own models
  // alongside Smart Routing; Codex resolves its catalog inside the running CLI
  // so the pre-launch row offers only what the create call can express. With
  // the server flag off, Smart Routing is never an option.
  it.each([
    ["Claude Code", "a1", true, "Smart Routing", "Opus 4.8"],
    ["Claude Code", "a1", false, null, "Default"],
    ["Codex", "a2", true, "Smart Routing", "Default (databricks-gpt-5-5)"],
  ] as const)(
    "%s Model dropdown with the flag %s offers %s alongside %s",
    (_label, agentId, flag, routingOption, siblingOption) => {
      renderLanding({ smart_routing_enabled: flag });
      openAgentConfig(agentId);
      openSelect("new-chat-landing-config-model");
      if (routingOption === null) {
        expect(screen.queryByRole("option", { name: "Smart Routing" })).toBeNull();
      } else {
        expect(screen.getByRole("option", { name: routingOption })).toBeTruthy();
      }
      expect(screen.getByRole("option", { name: siblingOption })).toBeTruthy();
    },
  );

  // No Model row at all: a non-routable harness has nothing to offer even with
  // the flag on, and its own knob row is untouched. (Codex always has a row —
  // the host resolves its launch catalog — see the dropdown cases above.)
  it.each([
    [
      "a non-routable harness with the flag on",
      "a_cursor",
      true,
      {
        id: "a_cursor",
        name: "cursor-native-ui",
        display_name: "Cursor",
        description: null,
        harness: "cursor-native",
        skills: [],
      } as AvailableAgent,
      "new-chat-landing-config-cursor-mode",
    ],
  ] as const)("gives %s no Model row", (_case, agentId, flag, agent, siblingTestId) => {
    if (agent) mockAgents([agent]);
    renderLanding({ smart_routing_enabled: flag });
    openAgentConfig(agentId);
    expect(screen.queryByTestId("new-chat-landing-config-model")).toBeNull();
    expect(screen.getByTestId(siblingTestId)).toBeTruthy();
  });

  // Per-family gateway gating: the apply layer rewrites the model through the
  // workspace AI gateway, so a family the host doesn't back there can't be
  // routed — and each dialog gates on its OWN family only.
  it.each([
    ["Claude Code", "a1", { "claude-native": false }, false],
    ["Claude Code", "a1", { "claude-native": true }, true],
    ["Claude Code", "a1", { "codex-native": false }, true],
    ["Codex", "a2", { "codex-native": false }, false],
    ["Codex", "a2", { "codex-native": true }, true],
    ["Codex", "a2", { "claude-native": false }, true],
  ] as const)(
    "%s Model dropdown with gateway_inference %j offers Smart Routing: %s",
    (_label, agentId, gateway, offered) => {
      mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
      renderLanding({ smart_routing_enabled: true });
      openAgentConfig(agentId);
      openSelect("new-chat-landing-config-model");
      if (offered) {
        expect(screen.getByRole("option", { name: "Smart Routing" })).toBeTruthy();
      } else {
        // The row stays — it still names the harness's own models — but the
        // routing sentinel is gone.
        expect(screen.queryByRole("option", { name: "Smart Routing" })).toBeNull();
        expect(
          screen.getByRole("option", {
            name: agentId === "a2" ? "databricks-gpt-5-6" : "Opus 4.8",
          }),
        ).toBeTruthy();
      }
    },
  );

  // The gateway only gates the EXTERNAL router. With the built-in judge
  // configured it answers for the off-gateway family instead, so the option the
  // cases above hid comes back.
  it.each([
    ["Claude Code", "a1", { "claude-native": false }],
    ["Codex", "a2", { "codex-native": false }],
  ] as const)(
    "%s Model dropdown offers Smart Routing off the gateway when the built-in judge can answer",
    (_label, agentId, gateway) => {
      mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
      renderLanding({
        smart_routing_enabled: true,
        smart_routing_sources: { external: true, oss: true },
      });
      openAgentConfig(agentId);
      openSelect("new-chat-landing-config-model");
      expect(screen.getByRole("option", { name: "Smart Routing" })).toBeTruthy();
    },
  );

  // No external router at all: the judge alone still routes both families,
  // gateway backing or not.
  it.each([
    ["a family the host keeps off the gateway", { "claude-native": false }],
    ["a host that reports nothing", undefined],
  ] as const)("offers Smart Routing from the built-in judge alone with %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: true },
    });
    openAgentConfig("a1");
    openSelect("new-chat-landing-config-model");
    expect(screen.getByRole("option", { name: "Smart Routing" })).toBeTruthy();
  });

  // Neither source can answer, so the option goes even on a gateway-backed
  // host — the row follows the sources, not the gateway map.
  it("offers no Smart Routing when the server reports neither source", () => {
    mockHosts([
      {
        ...host("online"),
        gateway_inference: { "claude-native": true, "codex-native": true },
      } as Host,
    ]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: false },
    });
    openAgentConfig("a1");
    openSelect("new-chat-landing-config-model");
    expect(screen.queryByRole("option", { name: "Smart Routing" })).toBeNull();
    expect(screen.getByRole("option", { name: "Opus 4.8" })).toBeTruthy();
  });

  it("offers Smart Routing on a host that reports no gateway_inference at all", () => {
    // Older host / server: unknown must not gate the option away.
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("a1");
    openSelect("new-chat-landing-config-model");
    expect(screen.getByRole("option", { name: "Smart Routing" })).toBeTruthy();
    closeMenu();
    openAgentConfig("a2");
    expect(screen.getByTestId("new-chat-landing-config-model")).toBeTruthy();
  });

  it("freezes Effort to an em-dash while Smart Routing is selected", () => {
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("a1");
    // Pin an effort first, so the em-dash can't pass vacuously on an unset row.
    pickSelectOption("new-chat-landing-config-effort", "High");
    expect(screen.getByTestId("new-chat-landing-config-effort").textContent).toContain("High");
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    const effort = screen.getByTestId("new-chat-landing-config-effort");
    expect(effort).toBeDisabled();
    expect(effort.textContent).toContain("—");
    expect(effort.textContent).not.toContain("High");
  });

  it("sends cost_control_mode_override 'on' and no model/effort override when routing is picked", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_routed" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    openAgentConfig("a1");
    // Pin a model + effort first so the routing pick has something to clear.
    pickSelectOption("new-chat-landing-config-model", "Opus 4.8");
    pickSelectOption("new-chat-landing-config-effort", "High");
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    saveConfig();

    const { body } = await submitAndReadBody();
    // Anchor on a required field so the absence checks can't pass vacuously.
    expect(body.agent_id).toBe("a1");
    expect(body.cost_control_mode_override).toBe("on");
    // The router picks the model (and its effort) per turn, so neither may be
    // pinned — a model_override would suppress per-turn routing server-side.
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });

  // The prompt rides along on a PINNED native create too, so the server routes
  // the model before the pane launches. Routing after the fact means holding
  // the first prompt inside the harness and replaying it, which the user sees
  // as their own message vanishing for seconds.
  it.each([
    ["Claude Code", "a1"],
    ["Codex", "a2"],
  ] as const)("sends the routing message on a pinned %s create", async (_label, agentId) => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_pinned_routed" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    openAgentConfig(agentId);
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    saveConfig();

    const { body } = await submitAndReadBody("refactor the auth module");
    expect(body.agent_id).toBe(agentId);
    expect(body.cost_control_mode_override).toBe("on");
    // Routes only — the real message is still delivered after navigation.
    expect(body.smart_routing_message).toBe("refactor the auth module");
    // The harness is the user's own pick — the bound wrapper agent names it —
    // so only the model is routed, and nothing pins one.
    expect(body.harness_override).not.toBe("auto");
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });

  it("sends no routing message on a pinned harness with routing off", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_pinned_plain" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    const { body } = await submitAndReadBody("refactor the auth module");
    expect(body.agent_id).toBe("a1");
    expect(body.cost_control_mode_override).toBeUndefined();
    expect(body.smart_routing_message).toBeUndefined();
  });

  // Sticky Smart Routing: the pick is remembered per harness in the same
  // localStorage store as the mode/model/effort knobs, so a returning user's
  // next session on that harness starts routed.

  it("preselects Smart Routing on a later session for the same harness", () => {
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("a1");
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    saveConfig();
    expect(
      JSON.parse(localStorage.getItem(HARNESS_OPTIONS_KEY) ?? "{}")["claude-native"],
    ).toMatchObject({ routing: "on" });

    remountLanding({ smart_routing_enabled: true });
    openAgentConfig("a1");
    expect(screen.getByTestId("new-chat-landing-config-model").textContent).toContain(
      "Smart Routing",
    );
  });

  it("remembers Codex's routing pick without touching Claude Code's", () => {
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("a2");
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    saveConfig();

    remountLanding({ smart_routing_enabled: true });
    openAgentConfig("a2");
    expect(screen.getByTestId("new-chat-landing-config-model").textContent).toContain(
      "Smart Routing",
    );
    closeMenu();
    // Claude Code never had routing picked, so it stays on Default.
    openAgentConfig("a1");
    const model = screen.getByTestId("new-chat-landing-config-model");
    expect(model.textContent).toContain("Default");
    expect(model.textContent).not.toContain("Smart Routing");
  });

  it("drops back to Default once the user picks a model again", () => {
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("a1");
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    saveConfig();
    openAgentConfig("a1");
    openSelect("new-chat-landing-config-model");
    // By role, not text: "Default" also labels the Permissions row's value.
    fireEvent.click(screen.getByRole("option", { name: "Default" }));
    saveConfig();

    remountLanding({ smart_routing_enabled: true });
    openAgentConfig("a1");
    const model = screen.getByTestId("new-chat-landing-config-model");
    expect(model.textContent).toContain("Default");
    expect(model.textContent).not.toContain("Smart Routing");
  });

  it("falls back to Default when a remembered routing pick meets a server without routing", () => {
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { routing: "on" } }),
    );
    renderLanding({ smart_routing_enabled: false });
    openAgentConfig("a1");
    const model = screen.getByTestId("new-chat-landing-config-model");
    expect(model.textContent).toContain("Default");
    expect(model.textContent).not.toContain("Smart Routing");
  });

  it("omits cost_control_mode_override when a remembered pick can't be honored", async () => {
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { routing: "on" } }),
    );
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_plain" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: false });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    const { body } = await submitAndReadBody();
    expect(body.agent_id).toBe("a1");
    expect(body.cost_control_mode_override).toBeUndefined();
  });

  it("launches a remembered routing pick as cost_control_mode_override 'on'", async () => {
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { routing: "on" } }),
    );
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_routed_again" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    const { body } = await submitAndReadBody();
    expect(body.cost_control_mode_override).toBe("on");
    expect(body.model_override).toBeUndefined();
  });

  it("clears a stale remembered model when Codex picks Smart Routing", async () => {
    // Codex's modal has no model picker of its own, so a model remembered under
    // codex-native has no other path out of the store. Left behind it rides
    // along with routing, and a session that carries both reads as
    // already-model-pinned server-side — routing then never runs.
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "codex-native": { model: "databricks-gpt-5-5", effort: "high" } }),
    );
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_codex_routed" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    openAgentConfig("a2");
    pickSelectOption("new-chat-landing-config-model", "Smart Routing");
    saveConfig();
    expect(
      JSON.parse(localStorage.getItem(HARNESS_OPTIONS_KEY) ?? "{}")["codex-native"],
    ).toMatchObject({ routing: "on", model: "", effort: "" });

    const { body } = await submitAndReadBody();
    expect(body.agent_id).toBe("a2");
    expect(body.cost_control_mode_override).toBe("on");
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });

  it("keeps a rehydrated routing pick when the model catalog resolves later", async () => {
    // The remembered model is validated against the host's catalog, which lands
    // after mount — so the seed runs again once it does. A remembered "route
    // every turn" must win both times, or the late seed re-pins the model and
    // silently downgrades the session the user thought was routed.
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { routing: "on", model: "opus" } }),
    );
    useHostModelOptionsMock.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    } as unknown as ReturnType<typeof useHostModelOptions>);
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_late_catalog" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    useHostModelOptionsMock.mockReturnValue({
      data: [{ id: "opus", model: "system.ai.claude-opus-4-8[1m]", displayName: "Opus 4.8" }],
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof useHostModelOptions>);
    // Any state change re-renders with the resolved catalog, re-running the seed.
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "ship it" },
    });
    openAgentConfig("a1");
    expect(screen.getByTestId("new-chat-landing-config-model").textContent).toContain(
      "Smart Routing",
    );
    closeMenu();

    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    const { body } = await readCreateBody();
    expect(body.agent_id).toBe("a1");
    expect(body.cost_control_mode_override).toBe("on");
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });
});

describe("NewChatLandingScreen Auto harness", () => {
  beforeEach(() => {
    setupLandingMocks();
    mockAgents([
      {
        id: "ag_polly",
        name: "polly",
        display_name: "Polly",
        description: null,
        harness: "pi",
        skills: [],
      },
    ]);
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  /** Select the Auto harness from the bundle agent's Agent Harness row. */
  function selectAutoHarness(): void {
    openAgentConfig("ag_polly");
    pickSelectOption("new-chat-landing-config-harness", "Smart Routing");
    saveConfig();
  }

  it("describes what Auto does next to its harness row", () => {
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("ag_polly");
    openSelect("new-chat-landing-config-harness");
    const auto = screen.getByTestId("new-chat-landing-harness-auto");
    expect(auto.textContent).toContain("Smart Routing");
    expect(auto.textContent).toContain("Harness and model picked per task by smart routing");
  });

  it("keeps naming the agent on the composer chip — the routed brain is its knob", () => {
    renderLanding({ smart_routing_enabled: true });
    selectAutoHarness();
    const chip = screen.getByTestId("new-chat-landing-agent-select");
    // The session still runs as Polly; routing her brain must not rewrite the
    // whole selection as if top-level Smart Routing had been picked.
    expect(chip.textContent).toContain("Polly");
    expect(chip.textContent).not.toContain("Smart Routing");
    // No router blurb either — that hover text belongs to the top-level pick.
    expect(chip).not.toHaveAttribute("title");
  });

  it("shows the harness row alone in the Auto config modal, still titled 'Configure Polly'", () => {
    renderLanding({ smart_routing_enabled: true });
    selectAutoHarness();
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-modal").textContent).toContain(
      "Configure Polly",
    );
    // Select it and that's it: a claude-sdk create carries no permission field,
    // so a locked Permissions row would be decoration.
    expect(screen.queryByTestId("new-chat-landing-config-permission")).toBeNull();
    // The row that selected Auto stays, reading the pick back — it is how the
    // user switches away without cancelling.
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Smart Routing",
    );
    // Every harness-specific knob is undecidable before the router picks.
    expect(screen.queryByTestId("new-chat-landing-config-model")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-config-effort")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-config-approval")).toBeNull();
  });

  it("keeps the routed brain when the agent's own row is re-picked", () => {
    renderLanding({ smart_routing_enabled: true });
    selectAutoHarness();
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Polly");
    // Re-clicking Polly is a pick of Polly, not a reset of her saved brain — she
    // was already selected, and the gear row is the way to switch away.
    selectAgent("ag_polly");
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Polly");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Smart Routing",
    );
  });

  it("sends harness_override 'auto' with cost_control_mode_override 'on'", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_auto" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    selectAutoHarness();
    const { body } = await submitAndReadBody();
    expect(body.harness_override).toBe("auto");
    expect(body.cost_control_mode_override).toBe("on");
  });

  it("sends no permission override for Auto, even after a stale mode was stored", async () => {
    // Permissions are inherited from the machine's own Claude Code / Codex
    // config, which is expressed by sending nothing: the permission mode rides
    // `terminal_launch_args` as ["--permission-mode", mode], and the default
    // omits the field entirely (same as launching claude-code on Default).
    localStorage.setItem(HARNESS_OPTIONS_KEY, JSON.stringify({ auto: { mode: "plan" } }));
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_auto" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    selectAutoHarness();
    const { raw, body } = await submitAndReadBody();
    // Anchor on a required field so the absence checks can't pass vacuously.
    expect(body.harness_override).toBe("auto");
    expect(body.terminal_launch_args).toBeUndefined();
    // No permission field of any spelling rides along.
    expect(raw).not.toContain("permission");
    expect(raw).not.toContain("plan");
  });
});

// ---------------------------------------------------------------------------
// Smart Routing as a top-level HARNESS row (no bundle agent). The router picks
// native Claude Code or Codex per task, so the row needs both CLIs ready. It
// binds the Claude wrapper as the create call's placeholder agent; the server
// rebinds to whichever wrapper the router picked.
// ---------------------------------------------------------------------------

describe("NewChatLandingScreen Smart Routing harness row", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  const SMART_ROUTING_ROW = "new-chat-landing-harness-smart-routing";

  function openPicker(): void {
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  }

  /** Pick the Smart Routing row out of the picker's Harnesses group. */
  function selectSmartRoutingHarness(): void {
    openPicker();
    fireEvent.click(screen.getByTestId(SMART_ROUTING_ROW));
  }

  it("offers the row in its own unlabeled group above the harnesses", () => {
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    const row = screen.getByTestId(SMART_ROUTING_ROW);
    // Plain label only — no helper blurb beside it.
    expect(row.textContent).toBe("Smart Routing");
    // The row leads the menu, above the "Harnesses" heading and its rows.
    const heading = screen.getByText("Harnesses");
    expect(row.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("hides the row when the server flag is off", () => {
    renderLanding({ smart_routing_enabled: false });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
    // The ordinary harness rows are still there, so this isn't vacuous.
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
  });

  it.each([
    ["codex not installed", { "claude-native": true, "codex-native": false }],
    ["claude not installed", { "claude-native": false, "codex-native": true }],
    ["codex needs auth", { "claude-native": true, "codex-native": "needs-auth" }],
  ])("hides the row when %s — a one-armed router is just that arm", (_case, configured) => {
    mockHosts([{ ...host("online"), configured_harnesses: configured } as Host]);
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
  });

  it("shows the row when the host reports both native CLIs ready", () => {
    mockHosts([
      {
        ...host("online"),
        configured_harnesses: { "claude-native": true, "codex-native": true },
      } as Host,
    ]);
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    expect(screen.getByTestId(SMART_ROUTING_ROW)).toBeTruthy();
  });

  // The five-arm menu needs both families on the workspace AI gateway, so
  // either one the host doesn't back takes the whole row away.
  it.each([
    ["codex isn't gateway-backed", { "claude-native": true, "codex-native": false }],
    ["claude isn't gateway-backed", { "claude-native": false, "codex-native": true }],
    ["neither is gateway-backed", { "claude-native": false, "codex-native": false }],
  ])("hides the row when %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
  });

  it.each([
    ["both families gateway-backed", { "claude-native": true, "codex-native": true }],
    ["the host reports nothing", undefined],
    ["the host reports another family only", { "claude-sdk": false }],
  ])("shows the row when %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    expect(screen.getByTestId(SMART_ROUTING_ROW)).toBeTruthy();
  });

  // The row launches a native pane whose harness the EXTERNAL router picks, so
  // the built-in judge cannot stand in for an off-gateway arm the way it does
  // for a per-harness model pick.
  it.each([
    ["codex isn't gateway-backed", { "claude-native": true, "codex-native": false }],
    ["claude isn't gateway-backed", { "claude-native": false, "codex-native": true }],
    ["neither is gateway-backed", { "claude-native": false, "codex-native": false }],
  ] as const)("hides the row when the judge is configured and %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: true, oss: true },
    });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
  });

  // The judge-only deployment: no external router, so the pane's harness pick
  // has nobody to make it. Gateway backing is beside the point.
  it.each([
    ["both families gateway-backed", { "claude-native": true, "codex-native": true }],
    ["the host reports nothing", undefined],
  ] as const)("hides the row on a judge-only server with %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: true },
    });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
    expect(screen.getByTestId("new-chat-landing-agent-a1")).toBeTruthy();
  });

  // Both sources, both arms on the gateway: the one shape that keeps the row —
  // and the shape a legacy server (no `smart_routing_sources`, just
  // `smart_routing_enabled: true`) resolves to, so it loses nothing.
  it("shows the row when the external router is configured alongside the judge", () => {
    mockHosts([
      {
        ...host("online"),
        gateway_inference: { "claude-native": true, "codex-native": true },
      } as Host,
    ]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: true, oss: true },
    });
    openPicker();
    expect(screen.getByTestId(SMART_ROUTING_ROW)).toBeTruthy();
  });

  // Neither source: the row goes, same as judge-only.
  it("hides the row when the server reports neither source", () => {
    mockHosts([
      {
        ...host("online"),
        gateway_inference: { "claude-native": true, "codex-native": true },
      } as Host,
    ]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: false },
    });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
  });

  it("announces the gateway as the cause when a host switch takes the row away", async () => {
    mockHosts([
      { ...host("online", 1), gateway_inference: { "claude-native": true, "codex-native": true } },
      { ...host("online", 2), gateway_inference: { "claude-native": true, "codex-native": false } },
    ] as Host[]);
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    expect(screen.queryByTestId("new-chat-landing-smart-routing-dropped")).toBeNull();

    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    const target = screen
      .getAllByText("machine-2")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    fireEvent.click(target!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("machine-2"),
    );

    const notice = await screen.findByTestId("new-chat-landing-smart-routing-dropped");
    expect(notice.textContent).toContain(
      "needs Codex running on the workspace AI gateway on machine-2",
    );
  });

  it("hides the row when only one native wrapper agent is registered", () => {
    mockAgents([
      {
        id: "a1",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
      },
    ]);
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
  });

  it("reads 'Smart Routing' on the composer chip and highlights only its own row", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    const chip = screen.getByTestId("new-chat-landing-agent-select");
    expect(chip.textContent).toContain("Smart Routing");
    // The placeholder wrapper must not be named — the router owns the pick.
    expect(chip.textContent).not.toContain("Claude Code");
    expect(chip).toHaveAttribute("title", "Harness and model picked per task by smart routing");
    // Two rows lit at once would misreport what runs.
    openPicker();
    expect(screen.getByTestId(SMART_ROUTING_ROW)).toHaveAttribute("data-active", "true");
    expect(screen.getByTestId("new-chat-landing-agent-a1")).not.toHaveAttribute("data-active");
  });

  it("shows only a locked Permissions row, titled 'Configure Smart Routing'", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByText("Configure Smart Routing")).toBeTruthy();
    const permission = screen.getByTestId("new-chat-landing-config-permission");
    expect(permission.textContent).toContain("Default");
    expect(permission).toBeDisabled();
    // Every harness-specific knob is undecidable before the router picks.
    expect(screen.queryByTestId("new-chat-landing-config-model")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-config-effort")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-config-approval")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-config-harness")).toBeNull();
  });

  it("locks Permissions to Default — no other mode is selectable", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    const permission = screen.getByTestId("new-chat-landing-config-permission");
    expect(permission.textContent).toContain("Default");
    expect(permission).toBeDisabled();
    // A disabled trigger can't open, so no other mode is reachable.
    openSelect("new-chat-landing-config-permission");
    expect(screen.queryByRole("option", { name: "Plan" })).toBeNull();
    expect(screen.queryByRole("option", { name: "Bypass permissions" })).toBeNull();
  });

  it("ignores a non-default mode remembered for the wrapper it binds", () => {
    // The placeholder rides claude-native, whose remembered "plan" is live in
    // state when the row is picked. It must not surface on the locked row, and
    // must not survive the pick — the create sends no override either way.
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { mode: "plan" } }),
    );
    renderLanding({ smart_routing_enabled: true });
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-permission").textContent).toContain("Plan");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-cancel"));

    selectSmartRoutingHarness();
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    const permission = screen.getByTestId("new-chat-landing-config-permission");
    expect(permission.textContent).toContain("Default");
    expect(permission.textContent).not.toContain("Plan");
    // Reads the state behind the locked row, not the row's own constant: the
    // wrapper's full modal is back and shows the reset value.
    fireEvent.click(screen.getByTestId("new-chat-landing-config-cancel"));
    selectAgent("a1");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-permission").textContent).toContain(
      "Default",
    );
  });

  it("leaves Smart Routing by re-picking a harness row", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
      "Smart Routing",
    );
    selectAgent("a2");
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Codex");
  });

  it("sends harness_override 'auto' with the routing message and routing on", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_smart" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    selectSmartRoutingHarness();
    const { raw, body } = await submitAndReadBody("refactor the auth module");
    expect(body.harness_override).toBe("auto");
    expect(body.cost_control_mode_override).toBe("on");
    // The server routes from this text at create time; the real message is
    // still delivered after navigation.
    expect(body.smart_routing_message).toBe("refactor the auth module");
    // The placeholder the server rebinds off.
    expect(body.agent_id).toBe("a1");
    // Nothing that describes the placeholder's own CLI may ride along.
    expect(body.labels).toBeUndefined();
    expect(body.terminal_launch_args).toBeUndefined();
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
    expect(raw).not.toContain("permission");
  });

  it("sends no routing message once the pick moves off Smart Routing", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_codex" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    selectSmartRoutingHarness();
    selectAgent("a2");
    const { body } = await submitAndReadBody();
    expect(body.agent_id).toBe("a2");
    expect(body.harness_override).toBeUndefined();
    expect(body.smart_routing_message).toBeUndefined();
  });

  // Sticky Smart Routing: the row is a harness pick like any other, so it lands
  // in the same per-agent last-harness store and a return visit starts on it.
  // When the row can't be offered on the next visit, the pick degrades to the
  // default selection rather than stranding a chip with no row behind it.

  /** Seed the store as a previous session that ended on Smart Routing. */
  function seedStoredSmartRouting(): void {
    localStorage.setItem(LAST_AGENT_KEY, "a1");
    localStorage.setItem(LAST_HARNESS_KEY, JSON.stringify({ a1: "auto-native" }));
  }

  it("stores the pick under the placeholder wrapper it binds", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({
      a1: "auto-native",
    });
    expect(localStorage.getItem(LAST_AGENT_KEY)).toBe("a1");
  });

  it("preselects Smart Routing on a later visit", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();

    remountLanding({ smart_routing_enabled: true });
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
      "Smart Routing",
    );
    openPicker();
    expect(screen.getByTestId(SMART_ROUTING_ROW)).toHaveAttribute("data-active", "true");
    expect(screen.getByTestId("new-chat-landing-agent-a1")).not.toHaveAttribute("data-active");
  });

  it.each([
    ["codex is missing on this host", { "claude-native": true, "codex-native": false }, true],
    ["claude is missing on this host", { "claude-native": false, "codex-native": true }, true],
    ["routing is disabled server-side", null, false],
  ] as const)(
    "falls back to the default harness when %s",
    (_case, configured, smartRoutingEnabled) => {
      seedStoredSmartRouting();
      if (configured) {
        mockHosts([{ ...host("online"), configured_harnesses: configured } as Host]);
      }
      renderLanding({ smart_routing_enabled: smartRoutingEnabled });
      // Exactly what an empty store would have given: the default harness pick.
      const chip = screen.getByTestId("new-chat-landing-agent-select");
      expect(chip.textContent).not.toContain("Smart Routing");
      expect(chip.textContent).toContain("Claude Code");
      // Silent: the user did nothing this visit to lose routing, and the row
      // the notice would talk about isn't in the picker either.
      expect(screen.queryByTestId("new-chat-landing-smart-routing-dropped")).toBeNull();
      openPicker();
      expect(screen.queryByTestId(SMART_ROUTING_ROW)).toBeNull();
      // The arm may come back, so the pick stays remembered.
      expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({
        a1: "auto-native",
      });
    },
  );

  it("announces the downgrade when a host switch takes Smart Routing away", async () => {
    // Dropping the pick silently reads as the UI forgetting it; the readiness
    // slot has to say Smart Routing went away and what runs instead.
    mockHosts([
      {
        ...host("online", 1),
        configured_harnesses: { "claude-native": true, "codex-native": true },
      },
      {
        ...host("online", 2),
        configured_harnesses: { "claude-native": true, "codex-native": false },
      },
    ] as Host[]);
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    expect(screen.queryByTestId("new-chat-landing-smart-routing-dropped")).toBeNull();

    // Switch to the host that has only one arm.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    const target = screen
      .getAllByText("machine-2")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    fireEvent.click(target!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("machine-2"),
    );
    const notice = await screen.findByTestId("new-chat-landing-smart-routing-dropped");
    expect(notice.textContent).toContain("Smart Routing");
    expect(notice.textContent).toContain("Claude Code");
    // Names the arm that's actually missing on the new host, not a generic
    // "both arms" line that would send the user looking at the wrong CLI.
    expect(notice.textContent).toContain("needs Codex ready on machine-2");
    expect(notice.textContent).not.toContain("Claude Code and Codex");

    // An explicit pick answers the notice — it stops nagging. (Codex folds into
    // the picker's "More" submenu on this host, so pick the ready arm.)
    selectAgent("a1");
    expect(screen.queryByTestId("new-chat-landing-smart-routing-dropped")).toBeNull();
  });

  it("yields the notice slot to the harness-readiness notice", async () => {
    // The fallback agent itself isn't ready on the new host, so the readiness
    // notice fires too. Two amber lines in one slot is noise — "set up this
    // harness" is the actionable one, so only it shows.
    mockHosts([
      {
        ...host("online", 1),
        configured_harnesses: { "claude-native": true, "codex-native": true },
      },
      {
        ...host("online", 2),
        configured_harnesses: { "claude-native": false, "codex-native": true },
      },
    ] as Host[]);
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();

    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    const target = screen
      .getAllByText("machine-2")
      .find((el) => el.closest('[role="menuitem"]') !== null);
    fireEvent.click(target!);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-host-chip").textContent).toContain("machine-2"),
    );

    expect(await screen.findByTestId("new-chat-landing-harness-warning")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-smart-routing-dropped")).toBeNull();
  });

  it("routes on the prompt the agent will receive, not the raw textarea value", async () => {
    // The router must classify the delivered prompt: sanitization (and the
    // mention preamble) happen before the create, not after it.
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_smart_sanitized" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    selectSmartRoutingHarness();
    const { body } = await submitAndReadBody("  refactor the auth\u0007 module  ");
    expect(body.smart_routing_message).toBe("refactor the auth module");
  });

  it("sends no routing on a create after the restored pick degraded", async () => {
    seedStoredSmartRouting();
    mockHosts([
      {
        ...host("online"),
        configured_harnesses: { "claude-native": true, "codex-native": false },
      } as Host,
    ]);
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_degraded" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    const { body } = await submitAndReadBody();
    expect(body.agent_id).toBe("a1");
    expect(body.harness_override).toBeUndefined();
    expect(body.smart_routing_message).toBeUndefined();
    expect(body.cost_control_mode_override).toBeUndefined();
  });

  it("forgets the pick once an explicit harness row replaces it", () => {
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    // The wrapper the sentinel is stored under: clicking its row is a pick of
    // the wrapper, so the sentinel must not come back on the next visit.
    selectAgent("a2");
    selectAgent("a1");
    expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({});

    remountLanding({ smart_routing_enabled: true });
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
      "Claude Code",
    );
  });

  it("leaves a bundle agent's remembered brain harness alone", () => {
    // The sentinel shares the store with per-agent brain-harness picks, so
    // writing/degrading it must not disturb another agent's entry.
    localStorage.setItem(LAST_HARNESS_KEY, JSON.stringify({ ag_polly: "openai-agents" }));
    renderLanding({ smart_routing_enabled: true });
    selectSmartRoutingHarness();
    expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({
      ag_polly: "openai-agents",
      a1: "auto-native",
    });
  });
});

describe("claude-code default permission mode (payload anchor for Auto)", () => {
  beforeEach(setupLandingMocks);
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("omits terminal_launch_args when the permission mode is left on Default", async () => {
    // The behavior Auto matches: Default = inherit the machine's own config, so
    // the create call carries no permission flag at all.
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_claude" }),
    } as unknown as Response);
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    selectAgent("a1");
    const { raw } = await submitAndReadBody();
    expect(JSON.parse(raw).agent_id).toBe("a1");
    expect(JSON.parse(raw).terminal_launch_args).toBeUndefined();
    expect(raw).not.toContain("permission");
  });
});
// ---------------------------------------------------------------------------
// Smart Routing on a BUNDLE agent (Debby / Polly). Their brain runs on
// claude-sdk — not one of the two routable native harnesses — so the per-turn
// "Smart Routing" Model option never applies to them. Their whole routing story
// is the gear modal's Agent Harness row, where picking Smart Routing hands the
// harness AND the model to the router. These cover the config menu that pick
// leaves behind: what stays selectable, what goes away, and what the create
// call carries.
// ---------------------------------------------------------------------------

describe("NewChatLandingScreen bundle-agent Smart Routing", () => {
  // The real shape of examples/debby and examples/polly: a bundle agent whose
  // brain harness (claude-sdk) is overridable per session.
  const BUNDLE_AGENTS: AvailableAgent[] = [
    {
      id: "ag_debby",
      name: "debby",
      display_name: "Debby",
      description: null,
      harness: "claude-sdk",
      skills: [],
    },
    {
      id: "ag_polly",
      name: "polly",
      display_name: "Polly",
      description: null,
      harness: "claude-sdk",
      skills: [],
    },
  ];

  beforeEach(() => {
    setupLandingMocks();
    mockAgents(BUNDLE_AGENTS);
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  /** Open <agentId>'s gear modal and turn Smart Routing on (draft, not saved). */
  function draftSmartRouting(agentId: string): void {
    openAgentConfig(agentId);
    pickSelectOption("new-chat-landing-config-harness", "Smart Routing");
  }

  const BOTH_BUNDLES = [
    ["Debby", "ag_debby"],
    ["Polly", "ag_polly"],
  ] as const;

  it.each(BOTH_BUNDLES)(
    "%s's config menu is the brain-harness row alone, led by Smart Routing",
    (name, agentId) => {
      renderLanding({ smart_routing_enabled: true });
      openAgentConfig(agentId);
      expect(screen.getByTestId("new-chat-landing-config-modal").textContent).toContain(
        `Configure ${name}`,
      );
      // claude-sdk isn't a routable native harness, so none of the per-harness
      // knobs (which is where the per-turn routing Model option lives) apply.
      expect(screen.queryByTestId("new-chat-landing-config-model")).toBeNull();
      expect(screen.queryByTestId("new-chat-landing-config-effort")).toBeNull();
      expect(screen.queryByTestId("new-chat-landing-config-approval")).toBeNull();
      const harness = screen.getByTestId("new-chat-landing-config-harness");
      expect(harness.textContent).toContain("Claude SDK");
      openSelect("new-chat-landing-config-harness");
      const auto = screen.getByTestId("new-chat-landing-harness-auto");
      expect(auto.textContent).toContain("Smart Routing");
      expect(auto.textContent).toContain("Harness and model picked per task by smart routing");
      // Leads the list — it's the recommended pick, not a footnote.
      const sdk = screen.getByTestId("new-chat-landing-harness-claude-sdk");
      expect(auto.compareDocumentPosition(sdk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    },
  );

  it("offers no Smart Routing row when the server flag is off", () => {
    renderLanding({ smart_routing_enabled: false });
    openAgentConfig("ag_debby");
    openSelect("new-chat-landing-config-harness");
    expect(screen.queryByTestId("new-chat-landing-harness-auto")).toBeNull();
    // The ordinary brains are still listed, so this isn't vacuous.
    expect(screen.getByTestId("new-chat-landing-harness-codex")).toBeTruthy();
  });

  // The fully-auto brain routes across the same two arms as the top-level
  // row, so it needs both on the workspace AI gateway — a codex pane running
  // off a personal subscription cannot run a routed pick.
  it.each([
    ["codex isn't gateway-backed", { "claude-native": true, "codex-native": false }],
    ["claude isn't gateway-backed", { "claude-native": false, "codex-native": true }],
  ])("offers no Smart Routing brain when %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("ag_debby");
    openSelect("new-chat-landing-config-harness");
    expect(screen.queryByTestId("new-chat-landing-harness-auto")).toBeNull();
    // The explicit brains stay — only the routed option needs the gateway.
    expect(screen.getByTestId("new-chat-landing-harness-codex")).toBeTruthy();
    expect(screen.getByTestId("new-chat-landing-harness-claude-sdk")).toBeTruthy();
  });

  it("keeps the Smart Routing brain when both arms are gateway-backed", () => {
    mockHosts([
      {
        ...host("online"),
        gateway_inference: { "claude-native": true, "codex-native": true },
      } as Host,
    ]);
    renderLanding({ smart_routing_enabled: true });
    openAgentConfig("ag_debby");
    openSelect("new-chat-landing-config-harness");
    expect(screen.getByTestId("new-chat-landing-harness-auto")).toBeTruthy();
  });

  // The split-credential states — one family on the workspace gateway, the other
  // on a personal subscription. The external router can't reach the off-gateway
  // arm, but the built-in judge can, so the fully-auto brain survives both.
  it.each([
    ["codex is off the gateway", { "claude-native": true, "codex-native": false }],
    ["claude is off the gateway", { "claude-native": false, "codex-native": true }],
  ])(
    "keeps the Smart Routing brain when %s and the built-in judge can answer",
    (_case, gateway) => {
      mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
      renderLanding({
        smart_routing_enabled: true,
        smart_routing_sources: { external: true, oss: true },
      });
      openAgentConfig("ag_debby");
      openSelect("new-chat-landing-config-harness");
      expect(screen.getByTestId("new-chat-landing-harness-auto")).toBeTruthy();
    },
  );

  // The judge-only deployment. A bundle agent's routed brain runs the judge's
  // harness pick, so this surface must NOT follow the native-pane row off a
  // server with no external router — whatever the gateway map says.
  it.each([
    ["both arms gateway-backed", { "claude-native": true, "codex-native": true }],
    ["neither arm gateway-backed", { "claude-native": false, "codex-native": false }],
    ["the host reports nothing", undefined],
  ])("keeps the Smart Routing brain on a judge-only server with %s", (_case, gateway) => {
    mockHosts([{ ...host("online"), gateway_inference: gateway } as Host]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: true },
    });
    openAgentConfig("ag_debby");
    openSelect("new-chat-landing-config-harness");
    expect(screen.getByTestId("new-chat-landing-harness-auto")).toBeTruthy();
  });

  // Sources decide, not the gateway map: with neither router configured the
  // fully-auto brain goes even on a fully gateway-backed host.
  it("offers no Smart Routing brain when the server reports neither source", () => {
    mockHosts([
      {
        ...host("online"),
        gateway_inference: { "claude-native": true, "codex-native": true },
      } as Host,
    ]);
    renderLanding({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: false },
    });
    openAgentConfig("ag_debby");
    openSelect("new-chat-landing-config-harness");
    expect(screen.queryByTestId("new-chat-landing-harness-auto")).toBeNull();
    expect(screen.getByTestId("new-chat-landing-harness-claude-sdk")).toBeTruthy();
  });

  it.each(BOTH_BUNDLES)(
    "keeps %s's harness row on the Smart Routing pick, and the modal still names the agent",
    (name, agentId) => {
      renderLanding({ smart_routing_enabled: true });
      draftSmartRouting(agentId);
      // The control that made the pick must not vanish under the cursor: it
      // reads the choice back and is the way to switch away.
      expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
        "Smart Routing",
      );
      // The pick is a knob on this agent, so the modal is still hers — only the
      // top-level Smart Routing harness retitles.
      expect(screen.getByTestId("new-chat-landing-config-modal").textContent).toContain(
        `Configure ${name}`,
      );
      // Select it and that's it: nothing else is decidable, and a claude-sdk
      // create carries no permission field, so no locked row is offered.
      expect(screen.queryByTestId("new-chat-landing-config-permission")).toBeNull();
      expect(screen.queryByTestId("new-chat-landing-config-model")).toBeNull();
      expect(screen.queryByTestId("new-chat-landing-config-effort")).toBeNull();
    },
  );

  it("switches back to an explicit brain harness without leaving the modal", () => {
    renderLanding({ smart_routing_enabled: true });
    draftSmartRouting("ag_debby");
    pickSelectOption("new-chat-landing-config-harness", "Codex");
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain("Codex");
    // A brain pick never adds rows, so the row set is the same either way.
    expect(screen.queryByTestId("new-chat-landing-config-permission")).toBeNull();
    expect(screen.getByTestId("new-chat-landing-config-modal").textContent).toContain(
      "Configure Debby",
    );
  });

  it("discards a Smart Routing draft on Cancel", () => {
    renderLanding({ smart_routing_enabled: true });
    draftSmartRouting("ag_debby");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-cancel"));
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Debby");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Claude SDK",
    );
  });

  it("reopens on the committed Smart Routing pick after Save", () => {
    renderLanding({ smart_routing_enabled: true });
    draftSmartRouting("ag_debby");
    saveConfig();
    // The chip still names Debby — the routed brain is her knob, not a different
    // selection. (This is the leak the whole scope fix is about.)
    const chip = screen.getByTestId("new-chat-landing-agent-select");
    expect(chip.textContent).toContain("Debby");
    expect(chip.textContent).not.toContain("Smart Routing");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-modal").textContent).toContain(
      "Configure Debby",
    );
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Smart Routing",
    );
    expect(screen.queryByTestId("new-chat-landing-config-permission")).toBeNull();
  });

  it("mirrors the modal's rows in the gear tooltip", async () => {
    renderLanding({ smart_routing_enabled: true });
    draftSmartRouting("ag_debby");
    saveConfig();
    fireEvent.focus(screen.getByTestId("new-chat-landing-config-gear"));
    await waitFor(() =>
      expect(screen.getAllByTestId("new-chat-landing-config-gear-tooltip").length).toBeGreaterThan(
        0,
      ),
    );
    const tooltip = screen.getAllByTestId("new-chat-landing-config-gear-tooltip")[0];
    expect(tooltip.textContent).toContain("Agent Harness: Smart Routing");
    // The modal has no Permissions row for a routed brain, so the tooltip that
    // mirrors it must not invent one.
    expect(tooltip.textContent).not.toContain("Permissions");
  });

  it.each(BOTH_BUNDLES)(
    "sends harness_override 'auto' with routing on and no pinned model for %s",
    async (_name, agentId) => {
      authenticatedFetchMock.mockResolvedValue({
        ok: true,
        json: async () => ({ id: "conv_bundle_auto" }),
      } as unknown as Response);
      renderLanding({ smart_routing_enabled: true });
      await waitFor(() =>
        expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
      );
      draftSmartRouting(agentId);
      saveConfig();
      const { raw, body } = await submitAndReadBody();
      expect(body.agent_id).toBe(agentId);
      expect(body.harness_override).toBe("auto");
      expect(body.cost_control_mode_override).toBe("on");
      // A pinned model would silently disable routing for the whole session.
      expect(body.model_override).toBeUndefined();
      expect(body.reasoning_effort).toBeUndefined();
      expect(body.labels).toBeUndefined();
      expect(body.terminal_launch_args).toBeUndefined();
      // A bundle agent arms at create and routes on the first message event —
      // its harness isn't decided yet, so there is nothing to route here.
      expect(body.smart_routing_message).toBeUndefined();
      expect(raw).not.toContain("permission");
    },
  );

  it("sends the explicit brain with no routing once the pick moves off Smart Routing", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_bundle_codex" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    draftSmartRouting("ag_debby");
    pickSelectOption("new-chat-landing-config-harness", "Codex");
    saveConfig();
    const { body } = await submitAndReadBody();
    expect(body.harness_override).toBe("codex");
    expect(body.cost_control_mode_override).toBeUndefined();
  });

  // Sticky Smart Routing: the pick is a brain-harness pick like any other, so it
  // lands in the per-agent last-harness store and a return visit starts on it.
  it("remembers the pick per bundle agent", () => {
    renderLanding({ smart_routing_enabled: true });
    draftSmartRouting("ag_debby");
    saveConfig();
    // Polly's own brain is untouched — the store is keyed per agent.
    expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({
      ag_debby: "auto",
    });
    selectAgent("ag_polly");
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Polly");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Claude SDK",
    );
  });

  it("preselects Smart Routing on a later visit", () => {
    localStorage.setItem(LAST_AGENT_KEY, "ag_debby");
    localStorage.setItem(LAST_HARNESS_KEY, JSON.stringify({ ag_debby: "auto" }));
    renderLanding({ smart_routing_enabled: true });
    // Restored as Debby-with-a-routed-brain, so the chip is hers; the gear row is
    // where the restored pick shows.
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Debby");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Smart Routing",
    );
  });

  it("drops a remembered Smart Routing pick when the server has routing off", async () => {
    // No "auto" row exists to select, so a restored pick would leave the harness
    // select blank with no way back — while the create still asked the server to
    // route. Degrade to the agent's own brain instead, silently: the user did
    // nothing this visit to lose it.
    localStorage.setItem(LAST_AGENT_KEY, "ag_debby");
    localStorage.setItem(LAST_HARNESS_KEY, JSON.stringify({ ag_debby: "auto" }));
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_bundle_degraded" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: false });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    // The chip names Debby whether or not the pick degraded (a routed brain never
    // renames the selection), so it can't carry this test — the gear row and the
    // create payload below are what pin the degrade.
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Debby");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Claude SDK",
    );
    fireEvent.click(screen.getByTestId("new-chat-landing-config-cancel"));

    const { body } = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_debby");
    expect(body.harness_override).toBeUndefined();
    expect(body.cost_control_mode_override).toBeUndefined();
    // The flag may come back on, so the stored pick stays put.
    expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({
      ag_debby: "auto",
    });
  });
});

// ---------------------------------------------------------------------------
// The two Smart Routing flavors share a label and must not share a scope. A
// bundle agent's routed brain (the "auto" sentinel, stored per agent) is a knob
// on that agent; the top-level Smart Routing harness ("auto-native", riding a
// placeholder wrapper) replaces the selection outright. These need one fixture
// carrying both — the two native wrappers AND a bundle agent — so a pick of one
// flavor can be shown not to move the other.
// ---------------------------------------------------------------------------

describe("NewChatLandingScreen Smart Routing flavors are scoped separately", () => {
  const SMART_ROUTING_ROW = "new-chat-landing-harness-smart-routing";

  beforeEach(() => {
    setupLandingMocks();
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
      {
        id: "ag_debby",
        name: "debby",
        display_name: "Debby",
        description: null,
        harness: "claude-sdk",
        skills: [],
      },
    ]);
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  function openPicker(): void {
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  }

  /** Give Debby a routed brain from her gear modal and commit it. */
  function routeDebbysBrain(): void {
    openAgentConfig("ag_debby");
    pickSelectOption("new-chat-landing-config-harness", "Smart Routing");
    saveConfig();
  }

  it("routing a bundle agent's brain leaves the top-level harness choice alone", () => {
    renderLanding({ smart_routing_enabled: true });
    routeDebbysBrain();
    // The session is still Debby's — this is the leak the fix closes: keying the
    // chip on the union of both flavors renamed everything "Smart Routing".
    const chip = screen.getByTestId("new-chat-landing-agent-select");
    expect(chip.textContent).toContain("Debby");
    expect(chip.textContent).not.toContain("Smart Routing");
    // The top-level row is offered here (both wrappers ready) and was NOT picked.
    openPicker();
    expect(screen.getByTestId(SMART_ROUTING_ROW)).not.toHaveAttribute("data-active");
    closeMenu();
    // Her brain pick is where it belongs: on her own Agent Harness row.
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Smart Routing",
    );
  });

  it("a plain new chat after that still starts on the native harness", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ id: "conv_native_after_brain" }),
    } as unknown as Response);
    renderLanding({ smart_routing_enabled: true });
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("repo"),
    );
    routeDebbysBrain();
    selectAgent("a1");
    const { body } = await submitAndReadBody();
    // Nothing of Debby's routed brain rides along with an unrelated harness pick.
    expect(body.agent_id).toBe("a1");
    expect(body.harness_override).toBeUndefined();
    expect(body.cost_control_mode_override).toBeUndefined();
    expect(body.smart_routing_message).toBeUndefined();
  });

  it("selecting a bundle agent after top-level Smart Routing hands the session to it", () => {
    renderLanding({ smart_routing_enabled: true });
    openPicker();
    fireEvent.click(screen.getByTestId(SMART_ROUTING_ROW));
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
      "Smart Routing",
    );
    // The reverse leak: the top-level sentinel must not read as Debby's brain.
    selectAgent("ag_debby");
    const chip = screen.getByTestId("new-chat-landing-agent-select");
    expect(chip.textContent).toContain("Debby");
    expect(chip.textContent).not.toContain("Smart Routing");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Claude SDK",
    );
    // The sentinel stays filed under the wrapper it bound, never under Debby.
    expect(JSON.parse(localStorage.getItem(LAST_HARNESS_KEY) ?? "{}")).toEqual({
      a1: "auto-native",
    });
  });

  it("a routed brain survives re-picking the agent's own row", () => {
    renderLanding({ smart_routing_enabled: true });
    routeDebbysBrain();
    // Re-clicking the already-selected agent clears the top-level sentinel (that
    // modal has no harness row to escape through) but must leave a saved brain
    // pick alone — the gear row is how you switch that one away.
    selectAgent("ag_debby");
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Debby");
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    expect(screen.getByTestId("new-chat-landing-config-harness").textContent).toContain(
      "Smart Routing",
    );
  });
});
