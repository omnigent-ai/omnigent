import { act, render, screen } from "@testing-library/react";
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

/** A panel beside an outside button, whose inert may be pending. */
function ClosingPanel({
  inert,
  pending,
  mounted = true,
  autoFocus = false,
}: {
  inert: boolean;
  pending: boolean;
  mounted?: boolean;
  autoFocus?: boolean;
}) {
  const ref = useInertRef<HTMLElement>(inert, undefined, { pending });
  return (
    <>
      <button type="button">Outside</button>
      {mounted ? (
        <aside ref={ref} aria-label="Panel">
          <button type="button" autoFocus={autoFocus}>
            Inside
          </button>
        </aside>
      ) : null}
    </>
  );
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

  describe("while inert is pending", () => {
    const inside = () => screen.getByRole("button", { name: "Inside", hidden: true });
    const outside = () => screen.getByRole("button", { name: "Outside" });

    it("moves focus out at once, as inert would", () => {
      const { rerender } = render(<ClosingPanel inert={false} pending={false} />);
      act(() => inside().focus());
      rerender(<ClosingPanel inert={false} pending />);
      expect(getPanel()).not.toHaveAttribute("inert");
      expect(document.activeElement).toBe(document.body);
    });

    it("sends focus that enters back where it came from, until inert lands", () => {
      const { rerender } = render(<ClosingPanel inert={false} pending />);
      act(() => outside().focus());
      act(() => inside().focus());
      expect(outside()).toHaveFocus();

      // Focus coming from nowhere outside just leaves.
      act(() => outside().blur());
      act(() => inside().focus());
      expect(document.activeElement).toBe(document.body);

      rerender(<ClosingPanel inert pending />);
      expect(getPanel()).toHaveAttribute("inert");
      rerender(<ClosingPanel inert={false} pending={false} />);
      act(() => inside().focus());
      expect(inside()).toHaveFocus();
    });
    it("keeps focus out of an element that mounts while inert is pending", () => {
      const { rerender } = render(<ClosingPanel inert={false} pending mounted={false} />);
      rerender(<ClosingPanel inert={false} pending />);
      act(() => outside().focus());
      act(() => inside().focus());
      expect(outside()).toHaveFocus();
    });

    it("blurs what an element focuses as it mounts while inert is pending", () => {
      const { rerender } = render(<ClosingPanel inert={false} pending mounted={false} />);
      rerender(<ClosingPanel inert={false} pending autoFocus />);
      expect(inside()).not.toHaveFocus();
      expect(document.activeElement).toBe(document.body);
    });

    it("lets an element that mounts as the guard lifts keep what it focuses", () => {
      const { rerender } = render(<ClosingPanel inert={false} pending mounted={false} />);
      rerender(<ClosingPanel inert={false} pending={false} autoFocus />);
      expect(inside()).toHaveFocus();
    });
  });

  it("marks an element that remounts while inert is unchanged", () => {
    const { rerender } = render(<Panel inert mounted={false} />);
    rerender(<Panel inert />);
    expect(getPanel()).toHaveAttribute("inert");
  });
});
