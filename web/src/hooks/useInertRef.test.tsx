import { render, screen } from "@testing-library/react";
import { type RefObject, useRef } from "react";
import { describe, expect, it } from "vitest";
import { useInertRef } from "./useInertRef";

function Panel({
  inert,
  forward,
  mounted = true,
}: {
  inert: boolean;
  forward?: (element: HTMLElement | null) => void;
  mounted?: boolean;
}) {
  const ref = useInertRef<HTMLElement>(inert, forward);
  return mounted ? (
    <aside ref={ref} aria-label="Panel">
      <button type="button">Inside</button>
    </aside>
  ) : null;
}

function PanelWithObjectRef({
  inert,
  holder,
}: {
  inert: boolean;
  holder: { ref?: RefObject<HTMLDivElement | null> };
}) {
  const objectRef = useRef<HTMLDivElement>(null);
  holder.ref = objectRef;
  return <div ref={useInertRef(inert, objectRef)} data-testid="panel" />;
}

const getPanel = () => screen.getByRole("complementary", { hidden: true });

describe("useInertRef", () => {
  it("sets the inert attribute that React 18 drops from the boolean prop", () => {
    const { rerender } = render(<Panel inert />);
    const panel = getPanel();
    expect(panel).toHaveAttribute("inert");

    rerender(<Panel inert={false} />);
    expect(panel).not.toHaveAttribute("inert");

    rerender(<Panel inert />);
    expect(panel).toHaveAttribute("inert");
  });

  it("keeps a forwarded callback ref attached while inert toggles", () => {
    const seen: (HTMLElement | null)[] = [];
    const forward = (element: HTMLElement | null) => {
      seen.push(element);
    };
    const { rerender, unmount } = render(<Panel inert forward={forward} />);
    const panel = getPanel();
    expect(seen).toEqual([panel]);

    rerender(<Panel inert={false} forward={forward} />);
    rerender(<Panel inert forward={forward} />);
    expect(seen).toEqual([panel]);
    expect(panel).toHaveAttribute("inert");

    unmount();
    expect(seen).toEqual([panel, null]);
  });

  it("assigns a forwarded object ref", () => {
    const holder: { ref?: RefObject<HTMLDivElement | null> } = {};
    const { rerender } = render(<PanelWithObjectRef inert holder={holder} />);
    const panel = screen.getByTestId("panel");
    expect(holder.ref?.current).toBe(panel);
    expect(panel).toHaveAttribute("inert");

    rerender(<PanelWithObjectRef inert={false} holder={holder} />);
    expect(holder.ref?.current).toBe(panel);
    expect(panel).not.toHaveAttribute("inert");
  });

  it("marks an element that remounts while inert is unchanged", () => {
    const { rerender } = render(<Panel inert mounted={false} />);
    rerender(<Panel inert />);
    expect(getPanel()).toHaveAttribute("inert");
  });
});
