import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BASE_MENU_TRIGGER_ATTRIBUTE } from "./DiffReviewBaseMenu";
import { useBaseMenuFocusHandoff } from "./useBaseMenuFocusHandoff";

const marker = { [BASE_MENU_TRIGGER_ATTRIBUTE]: "" };

/** Two reviews that each hold a base menu's trigger, swapped by `which`. */
function Reviews({ which }: { which: "first" | "second" | "none" }) {
  useBaseMenuFocusHandoff();
  return (
    <div>
      <button type="button">Elsewhere</button>
      <section>
        {which === "first" && (
          <button type="button" {...marker}>
            first
          </button>
        )}
        {which === "second" && (
          <button type="button" {...marker}>
            second
          </button>
        )}
      </section>
    </div>
  );
}

describe("useBaseMenuFocusHandoff", () => {
  it("moves focus to the trigger that replaced the focused one", () => {
    const view = render(<Reviews which="first" />);
    act(() => screen.getByRole("button", { name: "first" }).focus());
    view.rerender(<Reviews which="second" />);
    expect(screen.getByRole("button", { name: "second" })).toHaveFocus();
  });

  it("waits through a render with no trigger, then hands focus on", () => {
    const view = render(<Reviews which="first" />);
    act(() => screen.getByRole("button", { name: "first" }).focus());
    view.rerender(<Reviews which="none" />);
    expect(document.body).toHaveFocus();
    view.rerender(<Reviews which="second" />);
    expect(screen.getByRole("button", { name: "second" })).toHaveFocus();
  });

  it("leaves focus the person moved elsewhere", () => {
    const view = render(<Reviews which="first" />);
    act(() => screen.getByRole("button", { name: "first" }).focus());
    act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
    view.rerender(<Reviews which="second" />);
    expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();
  });

  it("forgets a trigger the person left for nowhere, so a later swap moves no focus", () => {
    const view = render(<Reviews which="first" />);
    const first = screen.getByRole("button", { name: "first" });
    act(() => first.focus());
    act(() => first.blur());
    view.rerender(<Reviews which="second" />);
    expect(screen.getByRole("button", { name: "second" })).not.toHaveFocus();
    expect(document.body).toHaveFocus();
  });
});
