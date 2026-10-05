import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ImportContextModal,
  type ImportContext,
  type ImportContextModalProps,
} from "./ImportContextModal";
import { MOCK_IMPORT_CONTEXT } from "./importContextMock";

// DialogContent reads isIOSShell to size modals for the iOS keyboard; keep it
// false so all tests run the standard browser path.
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => false,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderModal(
  context: ImportContext = MOCK_IMPORT_CONTEXT,
  props: Partial<Omit<ImportContextModalProps, "context">> = {},
) {
  return render(
    <ImportContextModal
      open={true}
      onOpenChange={vi.fn()}
      onConfirm={vi.fn()}
      {...props}
      context={context}
    />,
  );
}

function selectTab(tab: HTMLElement) {
  fireEvent.mouseDown(tab);
  fireEvent.focus(tab);
  fireEvent.click(tab);
}

function harnessTabs() {
  return within(screen.getByRole("tablist", { name: "Harness" })).getAllByRole("tab");
}

function switchHarness(name: string) {
  selectTab(within(screen.getByRole("tablist", { name: "Harness" })).getByRole("tab", { name }));
}

/** Asset-type tab labels ("MCPs 4", …) in the active harness. */
function assetTabNames() {
  const list = screen.queryByRole("tablist", { name: "Asset type" });
  return list
    ? within(list)
        .getAllByRole("tab")
        .map((t) => t.textContent)
    : [];
}

function switchAsset(label: string) {
  const list = screen.getByRole("tablist", { name: "Asset type" });
  selectTab(within(list).getByRole("tab", { name: new RegExp(`^${label}`) }));
}

/** Row names in the visible asset list. */
function rowNames(list: string) {
  return within(screen.getByRole("list", { name: list }))
    .getAllByRole("listitem")
    .map((row) => row.firstChild?.textContent);
}

describe("ImportContextModal – harness tabs", () => {
  it("lists one tab per detected harness and opens the first", () => {
    renderModal();

    const tabs = harnessTabs();
    expect(tabs.map((t) => t.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
    expect(screen.getByRole("tab", { name: "Claude Code" })).toBe(tabs[0]);
    expect(screen.getByRole("tab", { name: "Codex" })).toBe(tabs[1]);
    expect(screen.getByRole("tab", { name: "Cursor" })).toBe(tabs[2]);
    expect(screen.getByText("Your imports are ready")).toBeTruthy();
    expect(screen.getByText(/These carry over automatically\./)).toBeTruthy();
  });

  it("shows the credential line and read-only asset lists with details", () => {
    renderModal();

    expect(screen.getByText("Databricks AI Gateway")).toBeTruthy();
    expect(screen.getAllByText("Imported")).toHaveLength(1);
    expect(assetTabNames()).toEqual(["MCPs 5", "Skills 10", "Plugins 3"]);

    expect(rowNames("MCPs")).toEqual(["databricks-v2", "jira", "safe", "web-search", "figma"]);
    expect(screen.getByText("figma plugin · mcp.figma.com")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByText("Select all")).toBeNull();

    switchAsset("Plugins");
    expect(rowNames("Plugins")).toEqual(["frontend-toolkit", "dev-productivity", "figma"]);
    expect(screen.getByText("12 skills")).toBeTruthy();
    expect(screen.getByText("1 skill")).toBeTruthy();
  });

  it("switches to Codex and omits the empty Plugins type", () => {
    renderModal();
    switchHarness("Codex");

    expect(screen.getByText("Databricks (dbc-a5d4177a-49dc)")).toBeTruthy();
    expect(assetTabNames()).toEqual(["MCPs 2", "Skills 3"]);
    expect(rowNames("MCPs")).toEqual(["github", "web_search"]);
  });

  it("prefixes Claude skills with / and Codex skills with $", () => {
    renderModal();
    switchAsset("Skills");
    expect(rowNames("Skills")[0]).toBe("/create-kafka-topic");

    switchHarness("Codex");
    switchAsset("Skills");
    expect(rowNames("Skills")[0]).toBe("$code-review");
  });
});

describe("ImportContextModal – host and status", () => {
  it("names the host when given one", () => {
    renderModal(MOCK_IMPORT_CONTEXT, { hostName: "dev-laptop" });
    expect(screen.getByText(/Found in your harnesses on dev-laptop\./)).toBeTruthy();
  });

  it("shows a loading state instead of tabs", () => {
    renderModal(MOCK_IMPORT_CONTEXT, { status: "loading" });
    expect(screen.getByText("Checking your harnesses…")).toBeTruthy();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
  });

  it("explains an offline host", () => {
    renderModal(MOCK_IMPORT_CONTEXT, { status: "offline", hostName: "dev-laptop" });
    expect(
      screen.getByText("dev-laptop is offline. Reconnect it to review its imports."),
    ).toBeTruthy();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
  });

  it.each([false, true])(
    "keeps loaded assets and explains MCP errors (unsupported: %s)",
    (mcpUnsupported) => {
      renderModal({ ...MOCK_IMPORT_CONTEXT, mcps: [] }, { unavailable: ["mcps"], mcpUnsupported });

      expect(assetTabNames()).toEqual(["Skills 10", "Plugins 3"]);
      expect(screen.getByRole("status").textContent).toBe(
        mcpUnsupported
          ? "Please update this host to list MCP servers."
          : "Couldn't read MCP servers from this machine.",
      );
    },
  );

  it.each([false, true])(
    "explains all failures when nothing loaded (unsupported MCPs: %s)",
    (mcpUnsupported) => {
      renderModal(
        { credentials: [], mcps: [], skills: [], plugins: [] },
        { unavailable: ["mcps", "skills"], mcpUnsupported },
      );
      expect(
        screen.getByText(
          mcpUnsupported
            ? "Please update this host to list MCP servers. Couldn't read skills and plugins from this machine."
            : "Couldn't read MCP servers or skills and plugins from this machine.",
        ),
      ).toBeTruthy();
    },
  );
});

describe("ImportContextModal – empty states", () => {
  it("shows the empty state for a harness with a credential but no assets", () => {
    renderModal({ ...MOCK_IMPORT_CONTEXT, mcps: [], skills: [], plugins: [] });

    expect(harnessTabs()).toHaveLength(3);
    expect(screen.getByText("Databricks AI Gateway")).toBeTruthy();
    expect(screen.getByText("No MCPs, skills, or plugins detected")).toBeTruthy();
    expect(assetTabNames()).toEqual([]);
  });

  it("shows a single message and no tabs when nothing was detected", () => {
    renderModal({ credentials: [], mcps: [], skills: [], plugins: [] });

    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.getByText("Nothing to import from your harnesses")).toBeTruthy();
  });
});

describe("ImportContextModal – closing", () => {
  it("confirms and closes on Confirm", () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    renderModal(MOCK_IMPORT_CONTEXT, { onConfirm, onOpenChange });

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(onConfirm).toHaveBeenCalledWith();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("closes without confirming from the X button", () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    renderModal(MOCK_IMPORT_CONTEXT, { onConfirm, onOpenChange });

    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
