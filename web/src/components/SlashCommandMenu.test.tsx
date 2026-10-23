import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type McpServerRow,
  matchSlashCommandInvocation,
  BUILTIN_SLASH_COMMANDS,
  rankedSlashCommandNames,
  skillDisplayNames,
  skillMenuDescription,
  SlashCommandMenu,
} from "./SlashCommandMenu";

afterEach(cleanup);

describe("builtin command matching", () => {
  it("matches /new by its command name rather than a display alias", () => {
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "n")).toContain("/new");
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "sess")).not.toContain("/new");
  });
});

describe("matchSlashCommandInvocation", () => {
  const commands = [
    "/review",
    "/review-pr",
    "/Simplified Technical English",
    "/Simplified Technical English (ASD-STE100)",
  ];

  it("prefers the longest known name that ends at a word boundary", () => {
    expect(matchSlashCommandInvocation("/review-pr 123", commands)).toEqual({
      command: "/review-pr",
      args: "123",
    });
    expect(
      matchSlashCommandInvocation("/Simplified Technical English (ASD-STE100) now", commands),
    ).toEqual({ command: "/Simplified Technical English (ASD-STE100)", args: "now" });
    expect(matchSlashCommandInvocation("  /Simplified Technical English  ", commands)).toEqual({
      command: "/Simplified Technical English",
      args: "",
    });
  });

  it("rejects text that only shares a prefix with a known name", () => {
    expect(matchSlashCommandInvocation("/reviewer 123", commands)).toBeNull();
    expect(matchSlashCommandInvocation("/Simplified Technical", commands)).toBeNull();
    expect(matchSlashCommandInvocation("/Review-pr 123", commands)).toBeNull();
  });
});

const props = {
  query: "",
  activeIndex: -1,
  onSelect: vi.fn(),
  commands: { "/help": "Show help" },
};

describe("skill discovery state", () => {
  it("keeps Commands usable while the Skills section loads", () => {
    const onSelect = vi.fn();
    render(<SlashCommandMenu {...props} skillsStatus="loading" onSelect={onSelect} />);
    expect(screen.getByText("Commands")).toBeVisible();
    expect(screen.getByText("Skills")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Loading skills…");
    fireEvent.click(screen.getByTestId("slash-menu-item-help"));
    expect(onSelect).toHaveBeenCalledWith("/help");
  });

  it("shows loading with no matches and fills the open menu when skills arrive", () => {
    const { rerender } = render(
      <SlashCommandMenu {...props} query="review" skillsStatus="loading" />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading skills…");
    rerender(
      <SlashCommandMenu
        {...props}
        query="review"
        skillsStatus="ready"
        commands={{ ...props.commands, "/code-review": "Review code" }}
      />,
    );
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByTestId("slash-menu-item-code-review")).toBeVisible();
  });

  it("stops loading when discovery successfully finds no skills", () => {
    const { rerender } = render(<SlashCommandMenu {...props} skillsStatus="loading" />);
    rerender(<SlashCommandMenu {...props} skillsStatus="ready" />);
    expect(screen.getByRole("status")).toHaveTextContent("No skills available");
    expect(screen.queryByText("Loading skills…")).toBeNull();
  });

  it("keeps cached skills visible during a refresh or failure and offers retry", () => {
    const onRetrySkills = vi.fn();
    const commands = { "/review": "Review code" };
    const { rerender } = render(
      <SlashCommandMenu {...props} commands={commands} skillsStatus="loading" />,
    );
    expect(screen.getByTestId("slash-menu-item-review")).toBeVisible();
    rerender(
      <SlashCommandMenu
        {...props}
        commands={commands}
        skillsStatus="error"
        onRetrySkills={onRetrySkills}
      />,
    );
    expect(screen.getByTestId("slash-menu-item-review")).toBeVisible();
    expect(screen.queryByText("Loading skills…")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetrySkills).toHaveBeenCalledOnce();
  });

  it("shows disconnected state without a spinner", () => {
    render(<SlashCommandMenu {...props} skillsStatus="unavailable" />);
    expect(screen.getByRole("status")).toHaveTextContent("Skills unavailable while disconnected.");
  });

  it("preserves the existing empty-menu behavior when discovery state is absent", () => {
    const { container } = render(<SlashCommandMenu {...props} commands={{}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("skill display names", () => {
  it("types the directory name and labels the row with the frontmatter name", () => {
    const skill = {
      name: "asd-ste100",
      description: "Write in Simplified Technical English",
      display_name: "Simplified Technical English (ASD-STE100)",
    };
    const onSelect = vi.fn();
    render(
      <SlashCommandMenu
        {...props}
        query="asd"
        onSelect={onSelect}
        commands={{ "/asd-ste100": skillMenuDescription(skill) }}
      />,
    );
    const row = screen.getByTestId("slash-menu-item-asd-ste100");
    expect(row).toHaveTextContent(
      "/asd-ste100Simplified Technical English (ASD-STE100) — Write in Simplified Technical English",
    );
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith("/asd-ste100");
  });

  it("omits a display name that adds nothing beyond the command", () => {
    expect(skillMenuDescription({ name: "review", description: "Review code" })).toBe(
      "Review code",
    );
    expect(
      skillMenuDescription({ name: "review", description: "Review code", display_name: "review" }),
    ).toBe("Review code");
    expect(skillMenuDescription({ name: "ste", description: "", display_name: "STE" })).toBe("STE");
  });
});

describe("skill display name matching", () => {
  const skills = [
    {
      name: "asd-ste100",
      description: "STE",
      display_name: "Simplified Technical English (ASD-STE100)",
    },
    { name: "english-tutor", description: "Tutor", display_name: null },
  ];
  const commands = Object.fromEntries(skills.map((s) => [`/${s.name}`, s.description]));
  const labels = skillDisplayNames(skills, "/");

  it("finds a skill by its display name and selects the command", () => {
    expect(labels).toEqual({ "/asd-ste100": "Simplified Technical English (ASD-STE100)" });
    const onSelect = vi.fn();
    render(
      <SlashCommandMenu
        {...props}
        query="simplified"
        onSelect={onSelect}
        commands={commands}
        labels={labels}
      />,
    );
    fireEvent.click(screen.getByTestId("slash-menu-item-asd-ste100"));
    expect(onSelect).toHaveBeenCalledWith("/asd-ste100");
  });

  it("ranks command-name matches ahead of display-name-only matches", () => {
    expect(rankedSlashCommandNames(commands, "english", new Set(), labels)).toEqual([
      "/english-tutor",
      "/asd-ste100",
    ]);
    // Without labels, the display name is not searchable.
    expect(rankedSlashCommandNames(commands, "simplified", new Set())).toEqual([]);
  });
});

describe("/mcp panel connection state", () => {
  const menuProps = { query: "", activeIndex: -1, onSelect: vi.fn(), commands: {} };

  function renderPanel(overrides: Partial<Parameters<typeof SlashCommandMenu>[0]>) {
    return render(
      <MemoryRouter>
        <SlashCommandMenu {...menuProps} showMcpServers {...overrides} />
      </MemoryRouter>,
    );
  }

  it("labels connected and sign-in-required rows and leaves stateless rows unlabeled", () => {
    renderPanel({
      mcpServers: [
        { name: "github", description: "GitHub tools", connection: "connected" },
        { name: "tracker", connection: "sign-in-required", signIn: { kind: "inline" } },
        { name: "gone", connection: "unavailable" },
        { name: "local-files" },
      ],
      onMcpSignIn: vi.fn(),
    });
    expect(screen.getByTestId("mcp-server-row-github")).toHaveTextContent("Connected");
    expect(screen.getByTestId("mcp-server-row-tracker")).toHaveTextContent("Sign-in required");
    expect(screen.getByTestId("mcp-server-row-gone")).toHaveTextContent(
      "Not available to your account",
    );
    expect(screen.getByTestId("mcp-server-row-local-files")).toHaveTextContent(/^local-files$/);
    // Only the row that needs it offers the action.
    expect(screen.getAllByRole("button", { name: "Sign in" })).toHaveLength(1);
  });

  it("runs the inline sign-in for the row and shows its progress and failure", () => {
    const onMcpSignIn = vi.fn();
    const row: McpServerRow = {
      name: "tracker",
      connection: "sign-in-required",
      signIn: { kind: "inline" },
    };
    const panel = (extra: Partial<Parameters<typeof SlashCommandMenu>[0]> = {}) => (
      <MemoryRouter>
        <SlashCommandMenu
          {...menuProps}
          showMcpServers
          mcpServers={[row]}
          onMcpSignIn={onMcpSignIn}
          {...extra}
        />
      </MemoryRouter>
    );
    const { rerender } = render(panel());
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onMcpSignIn).toHaveBeenCalledWith("tracker");

    rerender(panel({ mcpAuthorizingName: "tracker" }));
    expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();

    rerender(
      panel({
        mcpSignInError: { name: "tracker", message: "Sign-in cancelled. Try again when ready." },
      }),
    );
    expect(
      within(screen.getByTestId("mcp-server-row-tracker")).getByRole("alert"),
    ).toHaveTextContent("Sign-in cancelled");
    expect(screen.getByRole("button", { name: "Sign in" })).not.toBeDisabled();
  });

  it("opens a host-managed sign-in page in a new tab", () => {
    renderPanel({
      mcpServers: [
        {
          name: "linear",
          connection: "sign-in-required",
          signIn: { kind: "url", url: "https://auth.example.com/start" },
        },
      ],
      onMcpSignIn: vi.fn(),
    });
    const link = screen.getByRole("link", { name: "Sign in" });
    expect(link).toHaveAttribute("href", "https://auth.example.com/start");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("disables every Sign in while one is running, labelling only the active row", () => {
    renderPanel({
      mcpServers: [
        { name: "tracker", connection: "sign-in-required", signIn: { kind: "inline" } },
        { name: "wiki", connection: "sign-in-required", signIn: { kind: "inline" } },
      ],
      onMcpSignIn: vi.fn(),
      mcpAuthorizingName: "tracker",
    });
    // A second popup would silently abort the first, so no row invites one.
    expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("offers nothing to click when a row needs sign-in but names no way to do it", () => {
    // A host server whose manager reported no usable URL: the state is still worth
    // showing, but the panel has no sign-in to run for it.
    renderPanel({
      mcpServers: [{ name: "linear", connection: "sign-in-required" }],
      onMcpSignIn: vi.fn(),
    });
    expect(screen.getByTestId("mcp-server-row-linear")).toHaveTextContent("Sign-in required");
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(screen.queryByRole("link", { name: /Sign in/ })).toBeNull();
  });

  it("sends a token- or workspace-based service to Settings instead of a popup", () => {
    renderPanel({
      mcpServers: [
        {
          name: "wiki",
          connection: "sign-in-required",
          signIn: { kind: "settings", href: "/settings/mcp" },
        },
      ],
      onMcpSignIn: vi.fn(),
    });
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(screen.getByRole("link", { name: "Sign in from Settings" })).toHaveAttribute(
      "href",
      "/settings/mcp",
    );
  });
});
