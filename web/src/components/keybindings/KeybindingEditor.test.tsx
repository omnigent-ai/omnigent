import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getKeybindingSnapshot, replaceAllUserKeybindings } from "@/actions";
import { resetKeybindingStoreForTesting } from "@/actions/KeybindingStore";
import { KeybindingEditor } from "./KeybindingEditor";

vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => children;
  const Select = ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (value: string) => void;
    children: ReactNode;
  }) => {
    const kids = Children.toArray(children);
    const trigger = kids.find((child) => isValidElement(child) && child.type === SelectTrigger);
    const testId =
      isValidElement(trigger) && typeof trigger.props === "object"
        ? (trigger.props as Record<string, unknown>)["data-testid"]
        : undefined;
    return (
      <select
        data-testid={typeof testId === "string" ? testId : undefined}
        value={value}
        onChange={(event) => onValueChange(event.target.value)}
      >
        {kids.filter((child) => !(isValidElement(child) && child.type === SelectTrigger))}
      </select>
    );
  };
  return {
    Select,
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: ReactNode }) => children,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <option value={value}>{children}</option>
    ),
  };
});

function renderEditor() {
  return render(<KeybindingEditor />);
}

function record(key: string, init: KeyboardEventInit = {}) {
  fireEvent.keyDown(screen.getByRole("application", { name: "Keybinding recorder" }), {
    key,
    ...init,
  });
}

beforeEach(() => {
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

describe("KeybindingEditor", () => {
  it("keeps action identifiers beside titles and uses icon controls", () => {
    renderEditor();
    const identifier = screen.getByText("session.action.new");
    expect(identifier).toBeInTheDocument();
    expect(identifier.parentElement).toHaveClass("flex");
    expect(
      screen.getByRole("button", { name: "Rebind session.action.new mod+n" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Rebind")).toBeNull();
    expect(screen.queryByText("Add alternate")).toBeNull();
    const modeFilter = screen.getByTestId("keybinding-mode-filter");
    expect(within(modeFilter).getByRole("option", { name: "Composer" })).toBeInTheDocument();
    expect(within(modeFilter).queryByRole("option", { name: "Markdown editor" })).toBeNull();
  });

  it("hides Escape-only actions", () => {
    renderEditor();
    fireEvent.change(screen.getByRole("textbox", { name: "Search keyboard shortcuts" }), {
      target: { value: "Stop response" },
    });
    expect(screen.queryByText("composer.action.stop")).toBeNull();
    expect(screen.getByText("No keyboard shortcuts found.")).toBeInTheDocument();
  });

  it("renders every complete shortcut in one clickable pill", () => {
    renderEditor();
    fireEvent.change(screen.getByRole("textbox", { name: "Search keyboard shortcuts" }), {
      target: { value: "Send message" },
    });
    const action = screen.getByText("composer.action.send").closest(".rounded-lg") as HTMLElement;
    expect(
      within(action).getByRole("button", { name: "Rebind composer.action.send Enter" }),
    ).toHaveTextContent("↵");
    expect(
      within(action).getByRole("button", { name: "Rebind composer.action.send primary+Enter" }),
    ).toHaveTextContent("Ctrl+↵");
    expect(
      within(action).getByRole("button", { name: "Rebind composer.action.send Enter" }),
    ).toHaveClass("cursor-pointer");
    expect(
      within(action).getAllByRole("button", { name: /^Rebind composer.action.send/ }),
    ).toHaveLength(2);
    const enterPill = within(action).getByRole("button", {
      name: "Rebind composer.action.send Enter",
    });
    const group = enterPill.closest('[data-slot="button-group"]') as HTMLElement;
    expect(
      within(group).getByRole("button", { name: "Remove composer.action.send Enter" }),
    ).toBeInTheDocument();
  });

  it("rebinds one pill without changing sibling shortcuts", () => {
    renderEditor();
    fireEvent.change(screen.getByRole("textbox", { name: "Search keyboard shortcuts" }), {
      target: { value: "Send message" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Rebind composer.action.send Enter" }));
    expect(screen.getByRole("dialog", { name: "Rebind keyboard shortcut" })).toBeInTheDocument();
    record("m", { code: "KeyM", ctrlKey: true });
    const rules = getKeybindingSnapshot().effectiveRules.filter(
      (rule) => rule.action === "composer.action.send",
    );
    expect(rules).toHaveLength(2);
    expect(rules.some((rule) => rule.origin === "user")).toBe(true);
    expect(rules.some((rule) => rule.id === "composer.send.primaryEnter")).toBe(true);
  });

  it("clicks an Unbound pill to assign an action with no default", () => {
    renderEditor();
    fireEvent.change(screen.getByRole("textbox", { name: "Search keyboard shortcuts" }), {
      target: { value: "Go to Inbox" },
    });
    const pill = screen.getByRole("button", {
      name: "Rebind workbench.action.navigateInbox unbound",
    });
    expect(pill).toHaveTextContent("Unbound");
    fireEvent.click(pill);
    record("i", { code: "KeyI", ctrlKey: true });
    expect(
      getKeybindingSnapshot().effectiveRules.find(
        (rule) => rule.action === "workbench.action.navigateInbox",
      ),
    ).toMatchObject({ origin: "user", mode: "global" });
  });

  it("replaces a removed shortcut with the same clickable Unbound pill", () => {
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Remove session.action.new mod+n" }));
    expect(screen.queryByRole("button", { name: "Rebind session.action.new mod+n" })).toBeNull();
    const action = screen.getByText("session.action.new").closest(".rounded-lg") as HTMLElement;
    expect(
      within(action).getByRole("button", { name: "Rebind session.action.new unbound" }),
    ).toHaveTextContent("Unbound");
    fireEvent.click(
      within(action).getByRole("button", { name: "Reset key binding for this action" }),
    );
    expect(
      screen.getByRole("button", { name: "Rebind session.action.new mod+n" }),
    ).toBeInTheDocument();
  });

  it("warns about conflicts and permits an intentional save", () => {
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Rebind session.action.new mod+n" }));
    record("k", { code: "KeyK", ctrlKey: true });
    const dialog = screen.getByRole("dialog", { name: "Shortcut already in use" });
    expect(within(dialog).getByText("New chat")).toBeInTheDocument();
    expect(within(dialog).getByText("Open command palette")).toBeInTheDocument();
    expect(within(dialog).getByText("New")).toBeInTheDocument();
    expect(within(dialog).getByText("Runs first")).toBeInTheDocument();
    expect(within(dialog).getByTestId("shortcut-conflict-card")).toHaveClass(
      "w-full",
      "min-w-0",
      "max-w-full",
      "overflow-hidden",
    );
    expect(
      within(dialog).getByText("Open command palette will run when both actions are available."),
    ).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Save anyway" });
    expect(confirm).toHaveClass("text-destructive");
    fireEvent.click(confirm);
    expect(
      getKeybindingSnapshot().effectiveRules.find((rule) => rule.id === "session.new"),
    ).toMatchObject({ origin: "user" });
  });

  it("restores focus after recording", async () => {
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Rebind session.action.new mod+n" }));
    record("n", { code: "KeyN", ctrlKey: true, shiftKey: true });
    await waitFor(() =>
      expect(document.activeElement?.getAttribute("aria-label")).toMatch(
        /^Rebind session.action.new/,
      ),
    );
  });

  it("searches title, action id, and formatted shortcut, then filters modes", () => {
    renderEditor();
    const search = screen.getByRole("textbox", { name: "Search keyboard shortcuts" });
    fireEvent.change(search, { target: { value: "Ctrl+N" } });
    expect(screen.getByText("session.action.new")).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "session.action.new" } });
    fireEvent.change(screen.getByTestId("keybinding-mode-filter"), {
      target: { value: "terminal" },
    });
    expect(screen.getByText("No keyboard shortcuts found.")).toBeInTheDocument();
  });

  it("resets one action or all overrides", () => {
    expect(
      replaceAllUserKeybindings([
        { id: "ui.one", action: "session.action.new", sequence: "ctrl+j", mode: "composer" },
        {
          id: "ui.two",
          action: "workbench.action.showCommands",
          sequence: "ctrl+l",
          mode: "terminal",
        },
      ]),
    ).toEqual({ ok: true, changed: true });
    renderEditor();
    const action = screen.getByText("session.action.new").closest(".rounded-lg") as HTMLElement;
    fireEvent.click(
      within(action).getByRole("button", { name: "Reset key binding for this action" }),
    );
    expect(getKeybindingSnapshot().userRules.map((rule) => rule.id)).toEqual(["ui.two"]);

    fireEvent.click(screen.getByRole("button", { name: "Reset all keyboard shortcuts" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset all" }));
    expect(getKeybindingSnapshot().userRules).toEqual([]);
  });

  it("surfaces persistence failures", () => {
    renderEditor();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    fireEvent.click(screen.getByRole("button", { name: "Remove session.action.new mod+n" }));
    expect(screen.getByRole("alert")).toHaveTextContent("could not be saved");
  });

  it("keeps same-tab store changes reactive", () => {
    renderEditor();
    act(() => {
      expect(
        replaceAllUserKeybindings([
          { id: "external", action: "session.action.new", sequence: "ctrl+j", mode: "global" },
        ]),
      ).toEqual({ ok: true, changed: true });
    });
    expect(
      screen.getByRole("button", { name: "Rebind session.action.new mod+n" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Rebind session.action.new ctrl+j" }),
    ).toBeInTheDocument();
  });
});
