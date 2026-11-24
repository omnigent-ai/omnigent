import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import { TERMINAL_SURFACE } from "@/lib/paneSurfaces";

import { TerminalPaneSurface, type TerminalPaneSurfaceProps } from "./TerminalPaneSurface";

// The terminal view reports its connection; its field is where a terminal reads keys.
const views = vi.hoisted(() => ({
  latest: null as null | {
    onStateChange?: (state: ConnectionState | null) => void;
    active?: boolean;
    focusOnConnect?: boolean;
  },
}));
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: (props: NonNullable<typeof views.latest>) => {
    views.latest = props;
    return <textarea aria-label="terminal input" />;
  },
}));

const TERMINAL = { id: "terminal_zsh_a" };

async function renderSurface(props: Partial<TerminalPaneSurfaceProps> = {}) {
  const element = (next: Partial<TerminalPaneSurfaceProps>) => (
    <TerminalPaneSurface
      conversationId="conv_surface"
      terminal={TERMINAL}
      readOnly={false}
      active
      focusOnConnect={() => false}
      {...next}
    />
  );
  const view = render(element(props));
  const input = await screen.findByLabelText("terminal input");
  const connect = () => act(() => views.latest?.onStateChange?.({ kind: "connected" }));
  return {
    input,
    connect,
    update: (next: Partial<TerminalPaneSurfaceProps>) =>
      view.rerender(element({ ...props, ...next })),
  };
}

afterEach(() => {
  cleanup();
  views.latest = null;
});

describe("TerminalPaneSurface", () => {
  it("marks its terminal as a pane's terminal surface, and decides connect focus itself", async () => {
    const { input } = await renderSurface();
    expect(input.closest(TERMINAL_SURFACE)).not.toBeNull();
    expect(views.latest?.focusOnConnect).toBe(false);
  });

  it("takes the keyboard as it connects only when the split's rule says so", async () => {
    const focusOnConnect = vi.fn(() => false);
    const refused = await renderSurface({ focusOnConnect });
    refused.connect();
    expect(focusOnConnect).toHaveBeenCalledOnce();
    expect(document.activeElement).not.toBe(refused.input);
    cleanup();

    const allowed = await renderSurface({ focusOnConnect: () => true });
    allowed.connect();
    expect(document.activeElement).toBe(allowed.input);
  });

  it("leaves the keyboard where it is when an inactive pane connects or reconnects", async () => {
    const outside = document.body.appendChild(document.createElement("input"));
    try {
      const { input, connect } = await renderSurface({
        active: false,
        focusOnConnect: () => true,
      });
      outside.focus();
      connect();
      expect(document.activeElement).not.toBe(input);
      expect(document.activeElement).toBe(outside);
    } finally {
      outside.remove();
    }
  });

  it("holds a focus request until the terminal connects, then takes it once", async () => {
    const taken = vi.fn();
    const { input, connect, update } = await renderSurface({
      focusRequest: 1,
      onFocusRequestTaken: taken,
    });
    expect(taken).not.toHaveBeenCalled();
    connect();
    expect(document.activeElement).toBe(input);
    expect(taken).toHaveBeenCalledExactlyOnceWith(1);
    update({ focusRequest: 1 });
    expect(taken).toHaveBeenCalledOnce();
  });

  it("leaves a request to a pane that isn't the focused one", async () => {
    const taken = vi.fn();
    const { connect } = await renderSurface({
      active: false,
      focusRequest: 1,
      onFocusRequestTaken: taken,
    });
    connect();
    expect(taken).not.toHaveBeenCalled();
  });

  it("passes every connection change on", async () => {
    const onStateChange = vi.fn();
    const { connect } = await renderSurface({ onStateChange });
    connect();
    act(() => views.latest?.onStateChange?.(null));
    expect(onStateChange.mock.calls).toEqual([[{ kind: "connected" }], [null]]);
  });
});
