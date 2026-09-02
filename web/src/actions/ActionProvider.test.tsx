import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ActionScope,
  ActionsProvider,
  actionScopeIdsFromElement,
  useActionAvailable,
  useActions,
  useAvailableActions,
  useInternalActionRuntime,
  usePaletteActions,
} from "./ActionProvider";
import { useRegisterAction } from "./useRegisterAction";
import { HANDLED, NOT_HANDLED } from "./types";

function ActionProbe({ run }: { run: () => void }) {
  const actions = useActions();
  const available = useAvailableActions();
  useRegisterAction("session.action.new", {
    run: () => {
      run();
      return HANDLED;
    },
  });
  return (
    <>
      <button
        type="button"
        onClick={() => actions.execute({ action: "session.action.new", source: "button" })}
      >
        Execute
      </button>
      <output>
        {available.some((action) => action.id === "session.action.new") ? "ready" : "empty"}
      </output>
    </>
  );
}

describe("ActionsProvider", () => {
  it("is inert outside a provider", () => {
    function OutsideProbe() {
      const actions = useActions();
      return (
        <button
          type="button"
          onClick={() => {
            const result = actions.execute({ action: "session.action.new", source: "button" });
            expect(result).toBe(NOT_HANDLED);
          }}
        >
          Outside
        </button>
      );
    }
    function RegistrationOnly() {
      useRegisterAction("session.action.new", { run: vi.fn() });
      return null;
    }
    function AvailabilityProbe() {
      return <output>{useActionAvailable("session.action.new") ? "leaked" : "inert"}</output>;
    }
    const first = render(<RegistrationOnly />);
    first.unmount();
    render(
      <>
        <OutsideProbe />
        <AvailabilityProbe />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Outside" }));
    expect(screen.getByText("inert")).toBeInTheDocument();
  });

  it("registers, lists, and executes actions", async () => {
    const run = vi.fn();
    render(
      <ActionsProvider>
        <ActionProbe run={run} />
      </ActionsProvider>,
    );
    await waitFor(() => expect(screen.getByText("ready")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(run).toHaveBeenCalledOnce();
  });

  it("invalidates live availability without re-registering the handler", async () => {
    function LiveProbe() {
      const [enabled, setEnabled] = useState(true);
      const available = useAvailableActions();
      useRegisterAction(
        "session.action.new",
        { isEnabled: () => enabled, run: () => HANDLED },
        enabled,
      );
      const action = available.find((candidate) => candidate.id === "session.action.new");
      return (
        <button type="button" onClick={() => setEnabled((value) => !value)}>
          {action?.enabled === false ? "disabled" : "enabled"}
        </button>
      );
    }
    render(
      <ActionsProvider>
        <LiveProbe />
      </ActionsProvider>,
    );
    await waitFor(() => expect(screen.getByText("enabled")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "enabled" }));
    await waitFor(() => expect(screen.getByText("disabled")).toBeInTheDocument());
  });

  it("does not subscribe a closed palette and refreshes when it opens", () => {
    function PaletteProbe({ open }: { open: boolean }) {
      const { actions } = usePaletteActions(open);
      return (
        <output>
          {actions.some((action) => action.id === "session.action.new") ? "listed" : "empty"}
        </output>
      );
    }
    let register = () => {};
    function RegistryProbe() {
      const runtime = useInternalActionRuntime();
      register = () => {
        runtime.registry.registerAction({
          action: "session.action.new",
          scopeId: null,
          run: () => HANDLED,
        });
      };
      return null;
    }
    const tree = (open: boolean) => (
      <ActionsProvider>
        <PaletteProbe open={open} />
        <RegistryProbe />
      </ActionsProvider>
    );
    const view = render(tree(false));
    expect(screen.getByText("empty")).toBeInTheDocument();
    act(() => register());
    expect(screen.getByText("empty")).toBeInTheDocument();
    view.rerender(tree(true));
    expect(screen.getByText("listed")).toBeInTheDocument();
  });

  it("keeps palette listing and execution on the pre-open focus", () => {
    const run = vi.fn();
    function PaletteProbe({ open }: { open: boolean }) {
      const palette = usePaletteActions(open);
      return (
        <button
          type="button"
          onClick={() => palette.executeAction("session.action.new")}
          disabled={!palette.actions.some((action) => action.id === "session.action.new")}
        >
          Palette action
        </button>
      );
    }
    function Registration() {
      useRegisterAction("session.action.new", {
        isVisible: ({ context }) => context.inputFocus,
        isEnabled: ({ context }) => context.inputFocus,
        run: () => {
          run();
          return HANDLED;
        },
      });
      return null;
    }
    const tree = (open: boolean) => (
      <ActionsProvider>
        <input aria-label="pre-open focus" />
        <PaletteProbe open={open} />
        <Registration />
      </ActionsProvider>
    );
    const view = render(tree(false));
    screen.getByRole("textbox", { name: "pre-open focus" }).focus();
    view.rerender(tree(true));
    const action = screen.getByRole("button", { name: "Palette action" });
    expect(action).toBeEnabled();
    action.focus();
    fireEvent.focusIn(action);
    expect(action).toBeEnabled();
    fireEvent.click(action);
    expect(run).toHaveBeenCalledOnce();
  });

  it("merges nested scope context and records scope ancestry on the DOM", () => {
    const seen = vi.fn();
    function ScopedProbe() {
      const actions = useActions();
      useRegisterAction("file.action.find", {
        run: (_invocation, { context }) => {
          seen(context);
          return HANDLED;
        },
      });
      return (
        <button
          type="button"
          onClick={(event) => {
            event.currentTarget.focus();
            actions.execute({ action: "file.action.find", source: "button" });
          }}
        >
          Find
        </button>
      );
    }
    render(
      <ActionsProvider>
        <ActionScope mode="fileViewer" context={{ fileSearchOpen: true }}>
          <div>
            <ActionScope mode="codeEditor" context={{ monacoFocus: true }}>
              <div>
                <ScopedProbe />
              </div>
            </ActionScope>
          </div>
        </ActionScope>
      </ActionsProvider>,
    );
    const button = screen.getByRole("button", { name: "Find" });
    expect(actionScopeIdsFromElement(button)).toHaveLength(2);
    fireEvent.click(button);
    expect(seen).toHaveBeenCalledWith(
      expect.objectContaining({ fileSearchOpen: true, monacoFocus: true }),
    );
  });

  it("rejects custom components that could drop the scope marker", () => {
    function CustomChild() {
      return <button type="button">Custom</button>;
    }
    expect(() =>
      render(
        <ActionsProvider>
          <ActionScope mode="composer">
            <CustomChild />
          </ActionScope>
        </ActionsProvider>,
      ),
    ).toThrow("ActionScope requires one intrinsic DOM element child");
  });

  it("stamps an existing child without inserting a layout wrapper", () => {
    render(
      <ActionsProvider>
        <div data-testid="row">
          <ActionScope mode="composer">
            <button type="button">Child</button>
          </ActionScope>
        </div>
      </ActionsProvider>,
    );
    const row = screen.getByTestId("row");
    expect(row.children).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Child" })).toHaveAttribute("data-action-scope");
  });
});
