import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImportContextModal } from "./ImportContextModal";
import { MOCK_IMPORT_CONTEXT } from "./importContextMock";
import type { ImportContext } from "./ImportContextModal";

// DialogContent reads isIOSShell to size modals for the iOS keyboard; keep it
// false so all tests run the standard browser path.
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => false,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function switchTab(name: string) {
  const tab = screen.getByRole("tab", { name });
  fireEvent.focus(tab);
  fireEvent.click(tab);
}

describe("ImportContextModal – credentials tab (default)", () => {
  it("renders the dialog title and credential rows with 'Imported' badges", () => {
    render(
      <ImportContextModal
        open={true}
        onOpenChange={vi.fn()}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );

    expect(screen.getByText("Your imports are ready")).toBeTruthy();

    // Harness label spans are present in the credential rows. SVG icons also
    // carry a <title> with the same text, so use getAllByText.
    expect(screen.getAllByText("Claude Code").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Codex").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Cursor").length).toBeGreaterThanOrEqual(1);

    // Each credential row carries an "Imported" status label.
    const imported = screen.getAllByText("Imported");
    expect(imported).toHaveLength(3);

    // Source strings are shown.
    expect(screen.getByText("Databricks AI Gateway")).toBeTruthy();
  });
});

describe("ImportContextModal – MCPs tab", () => {
  it("shows MCP rows checked by default, with tool-count metadata", () => {
    render(
      <ImportContextModal
        open={true}
        onOpenChange={vi.fn()}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );

    switchTab("MCPs");

    // All MCP checkboxes start checked.
    const checkboxes = screen.getAllByRole("checkbox");
    for (const cb of checkboxes) {
      expect(cb).toHaveAttribute("data-state", "checked");
    }

    // Singular "1 tool" for web-search (toolCount === 1).
    expect(screen.getByText("1 tool · Claude Code")).toBeTruthy();

    // Plural "9 tools" for confluence (toolCount === 9).
    expect(screen.getByText("9 tools · Cursor")).toBeTruthy();
  });
});

describe("ImportContextModal – confirm with partial selection", () => {
  it("calls onConfirm with remaining ids and then closes when items are unchecked", () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();

    render(
      <ImportContextModal
        open={true}
        onOpenChange={onOpenChange}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={onConfirm}
      />,
    );

    // Uncheck "confluence" on the MCPs tab.
    switchTab("MCPs");
    fireEvent.click(screen.getByRole("checkbox", { name: "confluence" }));

    // Uncheck "$create-kafka-topic" on the Skills tab.
    switchTab("Skills");
    fireEvent.click(screen.getByRole("checkbox", { name: "$create-kafka-topic" }));

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(onConfirm).toHaveBeenCalledOnce();
    const { mcps, skills } = onConfirm.mock.calls[0][0] as {
      mcps: string[];
      skills: string[];
    };

    // Deselected MCP must be absent; others preserved in input order.
    expect(mcps).not.toContain("cursor:confluence");
    expect(mcps).toContain("claude:databricks-v2");
    expect(mcps).toContain("codex:github");

    // Deselected skill must be absent; others preserved in input order.
    expect(skills).not.toContain("claude:create-kafka-topic");
    expect(skills).toContain("claude:create-system");

    // Dialog closes after confirming.
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("ImportContextModal – empty lists", () => {
  it("shows 'No skills detected' when the skills list is empty", () => {
    const emptySkillsContext: ImportContext = {
      ...MOCK_IMPORT_CONTEXT,
      skills: [],
    };

    render(
      <ImportContextModal
        open={true}
        onOpenChange={vi.fn()}
        context={emptySkillsContext}
        onConfirm={vi.fn()}
      />,
    );

    switchTab("Skills");

    expect(screen.getByText("No skills detected")).toBeTruthy();
  });
});

describe("ImportContextModal – selection reset on reopen", () => {
  it("resets all MCP checkboxes to checked after close then reopen", () => {
    const { rerender } = render(
      <ImportContextModal
        open={true}
        onOpenChange={vi.fn()}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );

    // Uncheck "confluence" while the modal is open.
    switchTab("MCPs");
    fireEvent.click(screen.getByRole("checkbox", { name: "confluence" }));
    expect(screen.getByRole("checkbox", { name: "confluence" })).toHaveAttribute(
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

    // Reopen — ImportContextBody remounts with fresh state.
    rerender(
      <ImportContextModal
        open={true}
        onOpenChange={vi.fn()}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );

    switchTab("MCPs");
    expect(screen.getByRole("checkbox", { name: "confluence" })).toHaveAttribute(
      "data-state",
      "checked",
    );
  });
});

describe("ImportContextModal – close button", () => {
  it("calls onOpenChange(false) when the X button is clicked", () => {
    const onOpenChange = vi.fn();

    render(
      <ImportContextModal
        open={true}
        onOpenChange={onOpenChange}
        context={MOCK_IMPORT_CONTEXT}
        onConfirm={vi.fn()}
      />,
    );

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
    };
    const onConfirm = vi.fn();
    render(
      <ImportContextModal open onOpenChange={vi.fn()} context={context} onConfirm={onConfirm} />,
    );

    switchTab("MCPs");
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
    expect(onConfirm).toHaveBeenCalledWith({ mcps: ["cursor:plugin-foo"], skills: [] });
  });
});
