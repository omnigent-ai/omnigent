// Tests for the Skill Registry API client — the wire→browser projection, the
// trust boolean ↔ `current`/`all-host` mapping, and error propagation. The
// client has no runtime fixture fallback: every failure (endpoint missing,
// network, auth, runner) must surface to the caller so the page shows its real
// error state.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BUNDLED_DISPLAY_PATH,
  catalogSourceRoots,
  getSkillCatalog,
  getSkillDetail,
  getSkillTrust,
  OWNERSHIP_ORDER,
  ownershipLabel,
  setSkillTrust,
  sourceRootKey,
  sourceRootRank,
  type SkillSummary,
} from "./skillsApi";
import { ApiError } from "./sessionsApi";
import * as identity from "./identity";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.mocked(identity.authenticatedFetch);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("getSkillCatalog", () => {
  it("projects the wire envelope to camelCase and passes the include flag", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [
          {
            id: "bundle:polly:cross-review",
            name: "cross-review",
            description: "Cross-review with peers.",
            origin: "built_in",
            ownership: "agent",
            agent_name: "polly",
            display_path: "Included with agent",
            enabled: true,
            available: true,
            has_conflict: false,
            updated_at: null,
          },
        ],
        include_other_tools: true,
        hidden_count: 2,
      }),
    );

    const catalog = await getSkillCatalog("sess_1", true);

    expect(fetchMock).toHaveBeenCalledWith("/v1/skills?session_id=sess_1&include_other_tools=true");
    expect(catalog.includeOtherTools).toBe(true);
    expect(catalog.hiddenCount).toBe(2);
    expect(catalog.skills[0]).toMatchObject({
      id: "bundle:polly:cross-review",
      name: "cross-review",
      origin: "built_in",
      // First-class ownership + agent name are projected to camelCase.
      ownership: "agent",
      agentName: "polly",
      // The wire `display_path` is projected to camelCase `displayPath`.
      displayPath: "Included with agent",
      hasConflict: false,
    });
  });

  it("propagates a 404 (endpoint absent) instead of masking it with fixtures", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { message: "nope" } }, false, 404));
    await expect(getSkillCatalog("sess_1", false)).rejects.toBeInstanceOf(ApiError);
  });

  it("propagates a 501 (endpoint not implemented) as an error", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { message: "unimplemented" } }, false, 501));
    await expect(getSkillCatalog("sess_1", false)).rejects.toBeInstanceOf(ApiError);
  });

  it("propagates a network failure (rejected fetch) instead of returning fixtures", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(getSkillCatalog("sess_1", false)).rejects.toThrow(/Failed to fetch/);
  });

  it("propagates a real server error (500)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { message: "boom" } }, false, 500));
    await expect(getSkillCatalog("sess_1", false)).rejects.toThrow();
  });
});

describe("getSkillDetail", () => {
  it("projects provenance + builds the conflict stack from selected_winner + candidates", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "workspace:test-generator",
        name: "test-generator",
        description: "Generate tests.",
        origin: "workspace",
        enabled: true,
        available: true,
        has_conflict: true,
        updated_at: null,
        content: "# test-generator",
        provenance: {
          provider: "omnigent",
          original_path: "./.agents/skills/test-generator",
          source_kind: "workspace generic",
          source_coords: "workspace:test-generator",
          digest: "9a02ff41",
        },
        selected_winner: "workspace:test-generator",
        conflict_candidates: ["personal:claude:frontend-toolkit/test-generator"],
        delivery: { mode: "automatic" },
      }),
    );

    const d = await getSkillDetail("workspace:test-generator", "sess_1", true);

    // Detail carries the SAME session + trust context as the list call.
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/skills/workspace%3Atest-generator?session_id=sess_1&include_other_tools=true",
    );
    expect(d.instructions).toBe("# test-generator");
    expect(d.advanced.discoveryProvider).toBe("omnigent");
    expect(d.advanced.originPath).toBe("./.agents/skills/test-generator");
    expect(d.advanced.delivery).toBe("Automatic");
    // Winner first (selected), then shadowed candidates.
    expect(d.advanced.conflicts).toEqual([
      { coords: "workspace:test-generator", selected: true },
      { coords: "personal:claude:frontend-toolkit/test-generator", selected: false },
    ]);
  });

  it("returns an empty conflict list when there is no conflict", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "bundle:ship",
        name: "ship",
        description: "Commit and PR.",
        origin: "built_in",
        enabled: true,
        available: true,
        has_conflict: false,
        updated_at: null,
        content: "# ship",
        provenance: {
          provider: "omnigent",
          original_path: "bundle://agent/skills/ship",
          source_kind: "agent bundle",
          source_coords: "bundle:ship",
          digest: "77b0c31d",
        },
        selected_winner: "bundle:ship",
        conflict_candidates: [],
        delivery: { mode: "automatic" },
      }),
    );

    const d = await getSkillDetail("bundle:ship", "sess_1", false);
    expect(d.advanced.conflicts).toEqual([]);
  });

  it("propagates a 404 for a missing skill instead of masking it", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { message: "not found" } }, false, 404));
    await expect(getSkillDetail("bundle:nope", "sess_1", false)).rejects.toBeInstanceOf(ApiError);
  });

  it("propagates a network failure (rejected fetch)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(getSkillDetail("bundle:ship", "sess_1", false)).rejects.toThrow(/Failed to fetch/);
  });
});

describe("trust setting", () => {
  it("reads include_other_tools from the trust envelope", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ value: "all-host", include_other_tools: true }));
    expect(await getSkillTrust()).toBe(true);
  });

  it("maps the boolean to the `value` body and returns the applied setting", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ value: "all-host", include_other_tools: true }));

    const applied = await setSkillTrust(true);

    expect(applied).toBe(true);
    const [, init] = fetchMock.mock.calls[0];
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual({ value: "all-host" });
  });

  it("maps false → current", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ value: "current", include_other_tools: false }));
    await setSkillTrust(false);
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init?.body as string)).toEqual({ value: "current" });
  });

  it("propagates a 404 when the trust endpoint is absent (no silent default)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, false, 404));
    await expect(getSkillTrust()).rejects.toBeInstanceOf(ApiError);
  });

  it("propagates a 404 on set instead of echoing the requested value", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, false, 404));
    await expect(setSkillTrust(true)).rejects.toBeInstanceOf(ApiError);
  });
});

describe("sourceRootKey", () => {
  const row = (displayPath: string, origin: "built_in" | "workspace" | "personal") => ({
    displayPath,
    origin,
  });

  it("groups a workspace skill at its `.../skills` root", () => {
    expect(sourceRootKey(row(".claude/skills/foo", "workspace"))).toBe(".claude/skills");
  });

  it("groups a home skill at its `~/.../skills` root, preserving the tilde", () => {
    expect(sourceRootKey(row("~/.codex/skills/migrate", "personal"))).toBe("~/.codex/skills");
    expect(sourceRootKey(row("~/.claude/skills/data-story", "personal"))).toBe("~/.claude/skills");
  });

  it("groups a deep workspace root at the `skills` boundary", () => {
    expect(sourceRootKey(row("omnigent/onboarding/agent/skills/build", "workspace"))).toBe(
      "omnigent/onboarding/agent/skills",
    );
  });

  it("collapses every bundled skill into one 'Included with agent' group", () => {
    expect(sourceRootKey(row(BUNDLED_DISPLAY_PATH, "built_in"))).toBe(BUNDLED_DISPLAY_PATH);
    // Origin wins even if a bundled path were somehow shaped like a real path.
    expect(sourceRootKey(row("anything", "built_in"))).toBe(BUNDLED_DISPLAY_PATH);
  });

  it("falls back to the parent dir for an unrooted path without a `skills/` marker", () => {
    expect(sourceRootKey(row("/abs/unrooted/foo", "personal"))).toBe("/abs/unrooted");
  });
});

describe("sourceRootRank", () => {
  it("orders bundled first, then workspace-relative, then home/absolute", () => {
    expect(sourceRootRank(BUNDLED_DISPLAY_PATH)).toBe(0);
    expect(sourceRootRank(".claude/skills")).toBe(1);
    expect(sourceRootRank("~/.codex/skills")).toBe(2);
    expect(sourceRootRank("/abs/unrooted")).toBe(2);
    // A concrete precedence chain the filter options rely on.
    expect(sourceRootRank(BUNDLED_DISPLAY_PATH)).toBeLessThan(sourceRootRank(".agents/skills"));
    expect(sourceRootRank(".agents/skills")).toBeLessThan(sourceRootRank("~/.claude/skills"));
  });
});

describe("catalogSourceRoots", () => {
  const sk = (displayPath: string, origin: SkillSummary["origin"]): SkillSummary => ({
    id: displayPath,
    name: "x",
    description: "",
    origin,
    ownership: origin === "built_in" ? "omnigent" : "local",
    agentName: null,
    displayPath,
    enabled: true,
    available: true,
    hasConflict: false,
    updatedAt: null,
  });

  it("returns the DISTINCT roots present, ordered bundled→workspace→home", () => {
    const roots = catalogSourceRoots([
      sk("~/.codex/skills/a", "personal"),
      sk(".claude/skills/b", "workspace"),
      sk(".claude/skills/c", "workspace"), // same root as b → deduped
      sk(BUNDLED_DISPLAY_PATH, "built_in"),
      sk("~/.claude/skills/d", "personal"),
    ]);
    expect(roots).toEqual([
      BUNDLED_DISPLAY_PATH,
      ".claude/skills",
      "~/.claude/skills",
      "~/.codex/skills",
    ]);
  });

  it("returns an empty list for an empty catalog", () => {
    expect(catalogSourceRoots([])).toEqual([]);
  });
});

describe("ownership grouping", () => {
  it("orders the sections Omnigent → Agent → Local", () => {
    expect(OWNERSHIP_ORDER).toEqual(["omnigent", "agent", "local"]);
  });

  it("labels each ownership category, folding the agent name into the Agent heading", () => {
    expect(ownershipLabel("omnigent")).toBe("Omnigent");
    expect(ownershipLabel("local")).toBe("Local");
    expect(ownershipLabel("agent", "polly")).toBe("Agent · polly");
    // No agent name → the bare "Agent" heading (never a vendor/path detail).
    expect(ownershipLabel("agent", null)).toBe("Agent");
    expect(ownershipLabel("agent")).toBe("Agent");
  });
});
