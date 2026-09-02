import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ActionScope,
  ActionsProvider,
  actionScopeIdsFromElement,
  useActionAvailable,
  useActions,
  useAvailableActions,
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
