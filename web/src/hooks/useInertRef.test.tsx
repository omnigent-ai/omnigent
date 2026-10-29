import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it } from "vitest";
import { useInertRef } from "./useInertRef";

function Panel({
  inert,
  forward,
}: {
  inert: boolean;
  forward?: (element: HTMLElement | null) => void;
}) {
  const ref = useInertRef<HTMLElement>(inert, forward);
  return (
    <aside ref={ref} aria-label="Panel">
      <button type="button">Inside</button>
    </aside>
  );
}

function PanelWithObjectRef({ inert }: { inert: boolean }) {
  const objectRef = useRef<HTMLDivElement>(null);
  const ref = useInertRef(inert, objectRef);
  return (
    <div ref={ref} data-testid="panel">
      <span data-testid="has-ref">{String(objectRef.current !== null)}</span>
    </div>
  );
}

describe("useInertRef", () => {
  it("sets the inert attribute that React 18 drops from the boolean prop", () => {
    const { rerender } = render(<Panel inert />);
    const panel = screen.getByRole("complementary", { hidden: true });
    expect(panel).toHaveAttribute("inert");

    rerender(<Panel inert={false} />);
    expect(panel).not.toHaveAttribute("inert");

    rerender(<Panel inert />);
    expect(panel).toHaveAttribute("inert");
  });

  it("also assigns a forwarded callback ref", () => {
    const seen: (HTMLElement | null)[] = [];
    const { unmount } = render(<Panel inert forward={(element) => seen.push(element)} />);
    expect(seen.at(-1)).toBeInstanceOf(HTMLElement);
    unmount();
    expect(seen.at(-1)).toBeNull();
  });

  it("also assigns a forwarded object ref", () => {
    const { rerender } = render(<PanelWithObjectRef inert />);
    rerender(<PanelWithObjectRef inert={false} />);
    expect(screen.getByTestId("panel")).not.toHaveAttribute("inert");
    expect(screen.getByTestId("has-ref")).toHaveTextContent("true");
  });
});
