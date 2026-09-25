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

function switchTab(harness: string) {
  const tab = screen.getByRole("tab", { name: harness });
  fireEvent.mouseDown(tab);
  fireEvent.focus(tab);
  fireEvent.click(tab);
}

function panel() {
  return within(screen.getByRole("tabpanel"));
}

function groupHeadings() {
  return panel()
    .queryAllByRole("heading", { level: 3 })
    .map((h) => h.textContent);
}

describe("ImportContextModal – harness tabs", () => {
  it("lists one tab per detected harness and opens the first", () => {
    renderModal();

    const tabs = screen.getAllByRole("tab");
    expect(screen.getByRole("tab", { name: "Claude Code" })).toBe(tabs[0]);
    expect(screen.getByRole("tab", { name: "Codex" })).toBe(tabs[1]);
    expect(screen.getByRole("tab", { name: "Cursor" })).toBe(tabs[2]);
    expect(tabs[0]).toHaveAttribute("data-state", "active");
    expect(screen.getByText("Your imports are ready")).toBeTruthy();
  });

  it("shows the Claude Code credential and only Claude assets, grouped", () => {
    renderModal();

    expect(panel().getByText("Databricks AI Gateway")).toBeTruthy();
    expect(panel().getAllByText("Imported")).toHaveLength(1);
    expect(groupHeadings()).toEqual(["MCPs 4", "Skills 10", "Plugins 2"]);

    expect(panel().getByRole("checkbox", { name: "databricks-v2" })).toBeTruthy();
    expect(panel().getByRole("checkbox", { name: "$create-kafka-topic" })).toBeTruthy();
    expect(panel().getByRole("checkbox", { name: "frontend-toolkit" })).toBeTruthy();
    expect(panel().queryByRole("checkbox", { name: "confluence" })).toBeNull();

    for (const cb of panel().getAllByRole("checkbox")) {
      expect(cb).toHaveAttribute("data-state", "checked");
    }
    expect(panel().getByText("18 tools")).toBeTruthy();
    expect(panel().getByText("1 tool")).toBeTruthy();
    expect(panel().getByText("12 skills")).toBeTruthy();
  });

  it("switches to Codex and hides the empty Plugins group", () => {
    renderModal();
    switchTab("Codex");

    expect(panel().getByText("Databricks (dbc-a5d4177a-49dc)")).toBeTruthy();
    expect(groupHeadings()).toEqual(["MCPs 2", "Skills 3"]);
    expect(panel().getByRole("checkbox", { name: "github" })).toBeTruthy();
    expect(panel().getByRole("checkbox", { name: "$ship" })).toBeTruthy();
    expect(panel().queryByRole("checkbox", { name: "databricks-v2" })).toBeNull();
  });
});

describe("ImportContextModal – confirm with partial selection", () => {
  it("keeps selections across tabs and returns the remaining ids", () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    renderModal(MOCK_IMPORT_CONTEXT, { onConfirm, onOpenChange });

    fireEvent.click(panel().getByRole("checkbox", { name: "$create-kafka-topic" }));
    fireEvent.click(panel().getByRole("checkbox", { name: "dev-productivity" }));

    switchTab("Cursor");
    fireEvent.click(panel().getByRole("checkbox", { name: "confluence" }));

    // Returning to a tab shows the earlier choice.
    switchTab("Claude Code");
    expect(panel().getByRole("checkbox", { name: "dev-productivity" })).toHaveAttribute(
      "data-state",
      "unchecked",
    );

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

    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(panel().getByText("Databricks AI Gateway")).toBeTruthy();
    expect(panel().getByText("No MCPs, skills, or plugins detected")).toBeTruthy();
    expect(groupHeadings()).toEqual([]);
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

    switchTab("Cursor");
    fireEvent.click(panel().getByRole("checkbox", { name: "confluence" }));
    expect(panel().getByRole("checkbox", { name: "confluence" })).toHaveAttribute(
      "data-state",
      "unchecked",
    );

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

    switchTab("Cursor");
    expect(panel().getByRole("checkbox", { name: "confluence" })).toHaveAttribute(
      "data-state",
      "checked",
    );
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

    expect(screen.getByRole("checkbox", { name: "plugin-foo" })).toHaveAttribute(
      "data-state",
      "checked",
    );
    expect(screen.getByRole("checkbox", { name: "plugin:foo" })).toHaveAttribute(
      "data-state",
      "unchecked",
    );
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledWith({
      mcps: ["cursor:plugin-foo"],
      skills: [],
      plugins: [],
    });
  });
});
