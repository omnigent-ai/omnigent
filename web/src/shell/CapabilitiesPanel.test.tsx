import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSessionCapabilities, type SessionCapabilities } from "@/hooks/useSessionCapabilities";
import { CapabilitiesPanel } from "./CapabilitiesPanel";

vi.mock("@/hooks/useSessionCapabilities", () => ({
  useSessionCapabilities: vi.fn(),
}));

const useSessionCapabilitiesMock = vi.mocked(useSessionCapabilities);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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
        skills: [{ name: "triage-issues", description: "Triage open GitHub issues." }],
        local_tools: [{ name: "sys_os_read", description: "Read a file." }],
      }),
    });

    expect(screen.getByText("triage-issues")).toBeInTheDocument();
    expect(screen.getByText("Triage open GitHub issues.")).toBeInTheDocument();
    expect(screen.getByText("sys_os_read")).toBeInTheDocument();
    expect(screen.getByText("Read a file.")).toBeInTheDocument();
  });

  it("renders MCP servers with transport and a deferred-tools note", () => {
    renderPanel({
      data: makeCapabilities({
        mcp_servers: [
          {
            name: "github",
            transport: "http",
            description: "GitHub API",
            url: "https://mcp.example/github",
            tools: [],
          },
        ],
      }),
    });

    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.getByText("http")).toBeInTheDocument();
    expect(screen.getByText("GitHub API")).toBeInTheDocument();
    // Per-server tool discovery is deferred, so the row notes it gracefully
    // rather than rendering an empty tool list.
    expect(screen.getByText("Inspect tools coming soon")).toBeInTheDocument();
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
          { name: "a", description: "x" },
          { name: "b", description: "y" },
        ],
      }),
    });

    // Skills header shows a count of 2.
    const skillsHeader = screen.getByText("Skills").closest("h3");
    expect(skillsHeader).not.toBeNull();
    expect(within(skillsHeader as HTMLElement).getByText("2")).toBeInTheDocument();
  });
});

describe("CapabilitiesPanel collapse / expand", () => {
  /** Grab a section header's collapsible trigger button by its title. */
  function triggerFor(title: string): HTMLButtonElement {
    return screen.getByRole("button", { name: new RegExp(title) }) as HTMLButtonElement;
  }

  it("renders every section expanded by default", () => {
    renderPanel({
      data: makeCapabilities({ skills: [{ name: "triage-issues", description: "Triage." }] }),
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
          { name: "triage-issues", description: "Triage." },
          { name: "review-pr", description: "Review." },
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
      data: makeCapabilities({ skills: [{ name: "triage-issues", description: "Triage." }] }),
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
        skills: [{ name: "triage-issues", description: "Triage." }],
        local_tools: [{ name: "sys_os_read", description: "Read a file." }],
      }),
    });

    fireEvent.click(triggerFor("Skills"));

    // Collapsing Skills leaves Local tools untouched.
    expect(screen.queryByText("triage-issues")).not.toBeInTheDocument();
    expect(screen.getByText("sys_os_read")).toBeInTheDocument();
    expect(triggerFor("Local tools")).toHaveAttribute("aria-expanded", "true");
  });
});
