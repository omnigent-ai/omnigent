import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";

import { usePendingFocus } from "./usePendingFocus";

let pending: ReturnType<typeof usePendingFocus<string>>;

function Harness() {
  const scope = useRef<HTMLDivElement>(null);
  pending = usePendingFocus<string>(scope);
  return (
    <>
      <div ref={scope}>
        <button type="button">Inside</button>
      </div>
      <input aria-label="Composer" />
    </>
  );
}

const target = () => pending.pending?.target ?? null;

describe("usePendingFocus", () => {
  it("holds a request until its target settles it", () => {
    render(<Harness />);
    act(() => pending.request("list"));
    expect(target()).toBe("list");
    act(() => pending.settle());
    expect(target()).toBeNull();
  });

  it("makes each request new, so asking again moves focus again", () => {
    render(<Harness />);
    act(() => pending.request("list"));
    const first = pending.pending;
    act(() => pending.request("list"));
    expect(pending.pending).not.toBe(first);
  });

  it("drops a request once the person types or clicks anywhere", () => {
    render(<Harness />);
    act(() => pending.request("list"));
    fireEvent.keyDown(document.body, { key: "a" });
    expect(target()).toBeNull();

    act(() => pending.request("list"));
    fireEvent.pointerDown(screen.getByRole("button", { name: "Inside" }));
    expect(target()).toBeNull();
  });

  it("drops a request when focus leaves its scope, and keeps it when focus stays inside", () => {
    render(<Harness />);
    act(() => pending.request("list"));
    act(() => screen.getByRole("button", { name: "Inside" }).focus());
    expect(target()).toBe("list");
    act(() => screen.getByRole("textbox", { name: "Composer" }).focus());
    expect(target()).toBeNull();
  });
});
