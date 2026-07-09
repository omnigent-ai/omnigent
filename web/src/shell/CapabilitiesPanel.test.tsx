import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  useSessionCapabilities,
  type CapabilityMcpServer,
  type CapabilitySkill,
  type CapabilityTool,
  type SessionCapabilities,
} from "@/hooks/useSessionCapabilities";
import { CapabilitiesPanel } from "./CapabilitiesPanel";

vi.mock("@/hooks/useSessionCapabilities", () => ({
  useSessionCapabilities: vi.fn(),
}));

const useSessionCapabilitiesMock = vi.mocked(useSessionCapabilities);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** A usable skill by default (in scope, not blocked); override per test. */
function makeSkill(overrides: Partial<CapabilitySkill> = {}): CapabilitySkill {
  return {
    name: "skill",
    description: "A skill.",
    source: "bundle",
    in_scope: true,
    blocked: false,
    ...overrides,
  };
}

/** A usable tool by default (not blocked); override per test. */
function makeTool(overrides: Partial<CapabilityTool> = {}): CapabilityTool {
  return {
    name: "tool",
    description: "A tool.",
    blocked: false,
    ...overrides,
  };
}

/** A connected server with no tools by default; override per test. */
function makeServer(overrides: Partial<CapabilityMcpServer> = {}): CapabilityMcpServer {
  return {
    name: "server",
    transport: "http",
    description: null,
    url: "https://mcp.example/server",
    status: "connected",
    error: null,
    tools: [],
    ...overrides,
  };
}

/** Minimal capabilities payload, overridable per test. */
function makeCapabilities(overrides: Partial<SessionCapabilities> = {}): SessionCapabilities {
  return {
    session_id: "conv_1",
    agent_id: "agent_1",
    sub_agent_name: null,
    skills: [],
    mcp_servers: [],
    local_tools: [],
    sub_agents: [],
    ...overrides,
  };
}

/** Drive the mocked hook and render the panel. */
function renderPanel(
  state: Partial<ReturnType<typeof useSessionCapabilities>> & {
    data?: SessionCapabilities | undefined;
  },
) {
  useSessionCapabilitiesMock.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
    ...state,
  } as unknown as ReturnType<typeof useSessionCapabilities>);
  render(<CapabilitiesPanel conversationId="conv_1" />);
}

/** Grab the panel-level "Only show usable" toggle switch. */
function scopeToggle(): HTMLElement {
  return screen.getByRole("switch", { name: /only show usable/i });
}

describe("CapabilitiesPanel loading / error states", () => {
  it("shows a loading state before any data arrives", () => {
    renderPanel({ isLoading: true, data: undefined });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails with no data", () => {
    renderPanel({ error: new Error("boom"), data: undefined });
    expect(screen.getByText("Failed to load capabilities.")).toBeInTheDocument();
  });
});

describe("CapabilitiesPanel sections", () => {
  it("renders empty states for every section when nothing is configured", () => {
    renderPanel({ data: makeCapabilities() });

    expect(screen.getByText("No skills available.")).toBeInTheDocument();
    expect(screen.getByText("No MCP servers configured.")).toBeInTheDocument();
    expect(screen.getByText("No local tools available.")).toBeInTheDocument();
    expect(screen.getByText("No sub-agents declared.")).toBeInTheDocument();
  });

  it("renders skills and local tools with name + description", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [makeSkill({ name: "triage-issues", description: "Triage open GitHub issues." })],
        local_tools: [makeTool({ name: "sys_os_read", description: "Read a file." })],
      }),
    });

    expect(screen.getByText("triage-issues")).toBeInTheDocument();
    expect(screen.getByText("Triage open GitHub issues.")).toBeInTheDocument();
    expect(screen.getByText("sys_os_read")).toBeInTheDocument();
    expect(screen.getByText("Read a file.")).toBeInTheDocument();
  });

  it("renders the sub-agent tree recursively, indenting nested descendants", () => {
    renderPanel({
      data: makeCapabilities({
        sub_agents: [
          {
            name: "researcher",
            description: "Digs through docs.",
            sub_agents: [{ name: "fetcher", description: "Fetches URLs.", sub_agents: [] }],
          },
        ],
      }),
    });

    const researcher = screen.getByText("researcher");
    const fetcher = screen.getByText("fetcher");
    expect(researcher).toBeInTheDocument();
    expect(fetcher).toBeInTheDocument();
    expect(screen.getByText("Digs through docs.")).toBeInTheDocument();
    expect(screen.getByText("Fetches URLs.")).toBeInTheDocument();

    // The nested child is indented deeper than its parent (tree read).
    const parentRow = researcher.closest("li");
    const childRow = fetcher.closest("li");
    const parentPad = Number.parseInt(
      parentRow?.getAttribute("style")?.match(/(\d+)px/)?.[1] ?? "0",
      10,
    );
    const childPad = Number.parseInt(
      childRow?.getAttribute("style")?.match(/(\d+)px/)?.[1] ?? "0",
      10,
    );
    expect(childPad).toBeGreaterThan(parentPad);
  });

  it("shows the section count badges", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [
          makeSkill({ name: "a", description: "x" }),
          makeSkill({ name: "b", description: "y" }),
        ],
      }),
    });

    // Skills header shows a count of 2.
    const skillsHeader = screen.getByText("Skills").closest("h3");
    expect(skillsHeader).not.toBeNull();
    expect(within(skillsHeader as HTMLElement).getByText("2")).toBeInTheDocument();
  });
});

describe("CapabilitiesPanel top-level scope filtering", () => {
  it("defaults the panel-level toggle on and filters skills, local tools, and MCP tools", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [
          makeSkill({ name: "usable-skill" }),
          makeSkill({ name: "scoped-out", in_scope: false }),
          makeSkill({ name: "blocked-skill", blocked: true }),
        ],
        local_tools: [
          makeTool({ name: "usable-tool" }),
          makeTool({ name: "blocked-tool", blocked: true }),
        ],
        mcp_servers: [
          makeServer({
            name: "github",
            tools: [
              makeTool({ name: "usable-mcp-tool" }),
              makeTool({ name: "blocked-mcp-tool", blocked: true }),
            ],
          }),
        ],
        sub_agents: [{ name: "helper", description: "Helps.", sub_agents: [] }],
      }),
    });

    expect(scopeToggle()).toBeChecked();

    // Skills: only usable shown.
    expect(screen.getByText("usable-skill")).toBeInTheDocument();
    expect(screen.queryByText("scoped-out")).not.toBeInTheDocument();
    expect(screen.queryByText("blocked-skill")).not.toBeInTheDocument();

    // Local tools: only non-blocked shown.
    expect(screen.getByText("usable-tool")).toBeInTheDocument();
    expect(screen.queryByText("blocked-tool")).not.toBeInTheDocument();

    // MCP tools: expand the server, only non-blocked tool shown.
    fireEvent.click(screen.getByText("github"));
    expect(screen.getByText("usable-mcp-tool")).toBeInTheDocument();
    expect(screen.queryByText("blocked-mcp-tool")).not.toBeInTheDocument();

    // Sub-agents are never gated by the toggle.
    expect(screen.getByText("helper")).toBeInTheDocument();
  });

  it("reveals blocked / out-of-scope entries with badges when toggled off", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [
          makeSkill({ name: "usable-skill" }),
          makeSkill({ name: "scoped-out", in_scope: false }),
          makeSkill({ name: "blocked-skill", blocked: true }),
        ],
        local_tools: [
          makeTool({ name: "usable-tool" }),
          makeTool({ name: "blocked-tool", blocked: true }),
        ],
        mcp_servers: [
          makeServer({
            name: "github",
            tools: [makeTool({ name: "blocked-mcp-tool", blocked: true })],
          }),
        ],
      }),
    });

    fireEvent.click(scopeToggle());
    expect(scopeToggle()).not.toBeChecked();

    // Skill badges.
    const scopedOutRow = screen.getByText("scoped-out").closest("li") as HTMLElement;
    expect(within(scopedOutRow).getByText("out of scope")).toBeInTheDocument();
    const blockedSkillRow = screen.getByText("blocked-skill").closest("li") as HTMLElement;
    expect(within(blockedSkillRow).getByText("blocked")).toBeInTheDocument();

    // Local tool badge.
    const blockedToolRow = screen.getByText("blocked-tool").closest("li") as HTMLElement;
    expect(within(blockedToolRow).getByText("blocked")).toBeInTheDocument();

    // MCP tool badge (after expanding).
    fireEvent.click(screen.getByText("github"));
    const blockedMcpRow = screen.getByText("blocked-mcp-tool").closest("li") as HTMLElement;
    expect(within(blockedMcpRow).getByText("blocked")).toBeInTheDocument();
  });

  it("reports usable counts in section badges regardless of the toggle", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [
          makeSkill({ name: "in-and-unblocked" }),
          makeSkill({ name: "in-but-blocked", blocked: true }),
          makeSkill({ name: "out-but-unblocked", in_scope: false }),
        ],
        local_tools: [makeTool({ name: "ok" }), makeTool({ name: "no", blocked: true })],
      }),
    });

    const skillsHeader = screen.getByText("Skills").closest("h3") as HTMLElement;
    expect(within(skillsHeader).getByText("1")).toBeInTheDocument();

    const toolsHeader = screen.getByText("Local tools").closest("h3") as HTMLElement;
    expect(within(toolsHeader).getByText("1")).toBeInTheDocument();
  });

  it("renders each skill's source as a label", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [makeSkill({ name: "user-skill", source: "user" })],
      }),
    });

    const row = screen.getByText("user-skill").closest("li") as HTMLElement;
    expect(within(row).getByText("user")).toBeInTheDocument();
  });

  it("hints at unavailable skills when none are in scope", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [
          makeSkill({ name: "scoped-out", in_scope: false }),
          makeSkill({ name: "blocked-skill", blocked: true }),
        ],
      }),
    });

    expect(
      screen.getByText("No skills in scope. Toggle off to see 2 unavailable skills."),
    ).toBeInTheDocument();

    fireEvent.click(scopeToggle());
    expect(screen.getByText("scoped-out")).toBeInTheDocument();
    expect(screen.getByText("blocked-skill")).toBeInTheDocument();
  });

  it("hints at blocked local tools when none are in scope", () => {
    renderPanel({
      data: makeCapabilities({
        local_tools: [makeTool({ name: "blocked-tool", blocked: true })],
      }),
    });

    expect(
      screen.getByText("No local tools in scope. Toggle off to see 1 blocked tool."),
    ).toBeInTheDocument();
  });
});

describe("CapabilitiesPanel MCP servers", () => {
  it("lists servers with transport and status, and is expandable to its tools", () => {
    renderPanel({
      data: makeCapabilities({
        mcp_servers: [
          makeServer({
            name: "github",
            transport: "http",
            description: "GitHub API",
            url: "https://mcp.example/github",
            status: "connected",
            tools: [makeTool({ name: "create_issue", description: "Open an issue." })],
          }),
        ],
      }),
    });

    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.getByText("http")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText("GitHub API")).toBeInTheDocument();

    // Tools are collapsed until the server row is clicked.
    expect(screen.queryByText("create_issue")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("github"));
    expect(screen.getByText("create_issue")).toBeInTheDocument();
    expect(screen.getByText("Open an issue.")).toBeInTheDocument();
  });

  it("surfaces a failed server's status and error", () => {
    renderPanel({
      data: makeCapabilities({
        mcp_servers: [
          makeServer({
            name: "broken",
            transport: "stdio",
            command: "./mcp-server",
            url: null,
            status: "failed",
            error: "connection refused",
            tools: [],
          }),
        ],
      }),
    });

    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
  });

  it("shows an empty state for a server with no discovered tools", () => {
    renderPanel({
      data: makeCapabilities({
        mcp_servers: [makeServer({ name: "empty", tools: [] })],
      }),
    });

    fireEvent.click(screen.getByText("empty"));
    expect(screen.getByText("No tools discovered.")).toBeInTheDocument();
  });

  it("shows an all-blocked hint within a server when the toggle is on", () => {
    renderPanel({
      data: makeCapabilities({
        mcp_servers: [
          makeServer({
            name: "locked",
            tools: [makeTool({ name: "danger", blocked: true })],
          }),
        ],
      }),
    });

    fireEvent.click(screen.getByText("locked"));
    expect(
      screen.getByText("No tools in scope. Toggle off to see 1 blocked tool."),
    ).toBeInTheDocument();
    expect(screen.queryByText("danger")).not.toBeInTheDocument();
  });
});

describe("CapabilitiesPanel collapse / expand", () => {
  /** Grab a section header's collapsible trigger button by its title. */
  function triggerFor(title: string): HTMLButtonElement {
    return screen.getByRole("button", { name: new RegExp(title) }) as HTMLButtonElement;
  }

  it("renders every section expanded by default", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [makeSkill({ name: "triage-issues", description: "Triage." })],
      }),
    });

    // Body content is visible and every trigger reports the expanded state.
    expect(screen.getByText("triage-issues")).toBeInTheDocument();
    for (const title of ["Skills", "MCP servers", "Local tools", "Sub-agents"]) {
      expect(triggerFor(title)).toHaveAttribute("aria-expanded", "true");
    }
  });

  it("collapses a section on header click, hiding its body but keeping the count", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [
          makeSkill({ name: "triage-issues", description: "Triage." }),
          makeSkill({ name: "review-pr", description: "Review." }),
        ],
      }),
    });

    const trigger = triggerFor("Skills");
    expect(screen.getByText("triage-issues")).toBeInTheDocument();

    fireEvent.click(trigger);

    // Body is gone, but the header — title, icon, and count badge — remains.
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("triage-issues")).not.toBeInTheDocument();
    expect(screen.getByText("Skills")).toBeInTheDocument();
    expect(within(trigger).getByText("2")).toBeInTheDocument();
  });

  it("re-expands a collapsed section on a second click", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [makeSkill({ name: "triage-issues", description: "Triage." })],
      }),
    });

    const trigger = triggerFor("Skills");

    fireEvent.click(trigger);
    expect(screen.queryByText("triage-issues")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("triage-issues")).toBeInTheDocument();
  });

  it("toggles sections independently", () => {
    renderPanel({
      data: makeCapabilities({
        skills: [makeSkill({ name: "triage-issues", description: "Triage." })],
        local_tools: [makeTool({ name: "sys_os_read", description: "Read a file." })],
      }),
    });

    fireEvent.click(triggerFor("Skills"));

    // Collapsing Skills leaves Local tools untouched.
    expect(screen.queryByText("triage-issues")).not.toBeInTheDocument();
    expect(screen.getByText("sys_os_read")).toBeInTheDocument();
    expect(triggerFor("Local tools")).toHaveAttribute("aria-expanded", "true");
  });
});
