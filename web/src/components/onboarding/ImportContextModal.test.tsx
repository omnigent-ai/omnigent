import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImportContextModal } from "./ImportContextModal";
import { MOCK_IMPORT_CONTEXT } from "./importContextMock";
import type { ImportContext, ImportSelection } from "./ImportContextModal";

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
  props: { onConfirm?: (s: ImportSelection) => void; onOpenChange?: (o: boolean) => void } = {},
) {
  return render(
    <ImportContextModal
      open={true}
      onOpenChange={props.onOpenChange ?? vi.fn()}
      context={context}
      onConfirm={props.onConfirm ?? vi.fn()}
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

function checkbox(name: string) {
  return screen.getByRole("checkbox", { name });
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
  });

  it("shows the credential line and asset-type tabs with counts", () => {
    renderModal();

    expect(screen.getByText("Databricks AI Gateway")).toBeTruthy();
    expect(screen.getAllByText("Imported")).toHaveLength(1);
    expect(assetTabNames()).toEqual(["MCPs 4", "Skills 10", "Plugins 2"]);

    // MCPs is selected first and lists only Claude's servers.
    expect(checkbox("databricks-v2")).toHaveAttribute("data-state", "checked");
    expect(screen.getByText("18 tools")).toBeTruthy();
    expect(screen.getByText("1 tool")).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "confluence" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "$create-system" })).toBeNull();

    switchAsset("Plugins");
    expect(checkbox("frontend-toolkit")).toHaveAttribute("data-state", "checked");
    expect(screen.getByText("12 skills")).toBeTruthy();
  });

  it("switches to Codex and omits the empty Plugins type", () => {
    renderModal();
    switchHarness("Codex");

    expect(screen.getByText("Databricks (dbc-a5d4177a-49dc)")).toBeTruthy();
    expect(assetTabNames()).toEqual(["MCPs 2", "Skills 3"]);
    expect(checkbox("github")).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "databricks-v2" })).toBeNull();
  });
});

describe("ImportContextModal – select all", () => {
  it("reflects partial selection and toggles the whole list", () => {
    renderModal();
    switchAsset("Skills");

    const all = checkbox("Select all");
    expect(all).toHaveAttribute("data-state", "checked");
    expect(screen.getByText("10 of 10 selected")).toBeTruthy();

    fireEvent.click(checkbox("$create-kafka-topic"));
    expect(all).toHaveAttribute("data-state", "indeterminate");
    expect(screen.getByText("9 of 10 selected")).toBeTruthy();

    // Clicking an indeterminate select-all selects everything again.
    fireEvent.click(all);
    expect(all).toHaveAttribute("data-state", "checked");
    expect(checkbox("$create-kafka-topic")).toHaveAttribute("data-state", "checked");

    fireEvent.click(all);
    expect(all).toHaveAttribute("data-state", "unchecked");
    expect(screen.getByText("0 of 10 selected")).toBeTruthy();
  });
});

describe("ImportContextModal – confirm with partial selection", () => {
  it("keeps selections across tabs and returns the remaining ids", () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    renderModal(MOCK_IMPORT_CONTEXT, { onConfirm, onOpenChange });

    switchAsset("Skills");
    fireEvent.click(checkbox("$create-kafka-topic"));
    switchAsset("Plugins");
    fireEvent.click(checkbox("dev-productivity"));

    switchHarness("Cursor");
    fireEvent.click(checkbox("confluence"));

    // Returning to a harness and type shows the earlier choice.
    switchHarness("Claude Code");
    switchAsset("Plugins");
    expect(checkbox("dev-productivity")).toHaveAttribute("data-state", "unchecked");

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(onConfirm).toHaveBeenCalledOnce();
    const { mcps, skills, plugins } = onConfirm.mock.calls[0][0] as ImportSelection;
    expect(mcps).not.toContain("cursor:confluence");
    expect(mcps).toHaveLength(MOCK_IMPORT_CONTEXT.mcps.length - 1);
    expect(skills).not.toContain("claude:create-kafka-topic");
    expect(skills).toContain("codex:ship");
    expect(plugins).toEqual(["claude:frontend-toolkit", "cursor:figma"]);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
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
    const onConfirm = vi.fn();
    renderModal({ credentials: [], mcps: [], skills: [], plugins: [] }, { onConfirm });

    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.getByText("Nothing to import from your harnesses")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledWith({ mcps: [], skills: [], plugins: [] });
  });
});

describe("ImportContextModal – selection reset on reopen", () => {
  it("resets all checkboxes to checked after close then reopen", () => {
    const { rerender } = renderModal();

    switchHarness("Cursor");
    fireEvent.click(checkbox("confluence"));
    expect(checkbox("confluence")).toHaveAttribute("data-state", "unchecked");

    // Close the modal (Radix unmounts ImportContextBody, discarding state).
    rerender(
      <ImportContextModal
        open={false}
        onOpenChange={vi.fn()}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );
    rerender(
      <ImportContextModal
        open={true}
        onOpenChange={vi.fn()}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );

    switchHarness("Cursor");
    expect(checkbox("confluence")).toHaveAttribute("data-state", "checked");
  });
});

describe("ImportContextModal – close button", () => {
  it("calls onOpenChange(false) when the X button is clicked", () => {
    const onOpenChange = vi.fn();
    renderModal(MOCK_IMPORT_CONTEXT, { onOpenChange });

    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("ImportContextModal – checkbox identity", () => {
  it("toggles only the clicked row when item ids differ only by punctuation", () => {
    const context: ImportContext = {
      credentials: [],
      mcps: [
        { id: "cursor:plugin-foo", name: "plugin-foo", harness: "cursor" },
        { id: "cursor:plugin:foo", name: "plugin:foo", harness: "cursor" },
      ],
      skills: [],
      plugins: [],
    };
    const onConfirm = vi.fn();
    renderModal(context, { onConfirm });

    fireEvent.click(screen.getByText("plugin:foo"));

    expect(checkbox("plugin-foo")).toHaveAttribute("data-state", "checked");
    expect(checkbox("plugin:foo")).toHaveAttribute("data-state", "unchecked");
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledWith({
      mcps: ["cursor:plugin-foo"],
      skills: [],
      plugins: [],
    });
  });
});
