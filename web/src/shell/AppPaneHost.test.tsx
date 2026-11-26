import { act, cleanup, render, screen } from "@testing-library/react";
import { createPortal } from "react-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppPaneHost } from "./AppPaneHost";
import { resetAppPanes, useAppPaneHosts } from "./appPanes";

const CONVERSATION = "conv_app_pane_host";
const TAB = "tab_files";

function Hosts({ onHosts }: { onHosts: (hosts: ReadonlyMap<string, HTMLElement>) => void }) {
  onHosts(useAppPaneHosts(CONVERSATION));
  return null;
}

interface HostProps {
  active?: boolean;
  focusOnActivate?: () => boolean;
  focusRequest?: number;
  onFocusRequestTaken?: (id: number) => void;
  onFocusIn?: () => void;
}

const hostElement = () => document.querySelector<HTMLElement>("[data-app-pane-host]")!;

function renderHost(props: HostProps = {}) {
  const element = (next: HostProps) => (
    <AppPaneHost
      conversationId={CONVERSATION}
      tabId={TAB}
      onFocusIn={next.onFocusIn ?? (() => {})}
      active={next.active ?? false}
      focusOnActivate={next.focusOnActivate ?? (() => true)}
      focusRequest={next.focusRequest}
      onFocusRequestTaken={next.onFocusRequestTaken}
    />
  );
  const view = render(element(props));
  return { ...view, update: (next: HostProps) => view.rerender(element({ ...props, ...next })) };
}

/** jsdom has no layout; the host only focuses what has a box. */
const giveBox = (element: HTMLElement | null) => {
  if (element) element.getClientRects = () => [new DOMRect(0, 0, 10, 10)] as unknown as DOMRectList;
};

/** Put a field in the host, as the sidebar renders a tab into it, and let the host see it. */
async function fillHost(content: HTMLElement = document.createElement("input")) {
  giveBox(content);
  await act(async () => void hostElement().append(content));
  return content;
}

afterEach(() => {
  cleanup();
  resetAppPanes();
});

describe("AppPaneHost", () => {
  it("publishes itself as the tab's host and withdraws when it unmounts", () => {
    let hosts: ReadonlyMap<string, HTMLElement> | undefined;
    render(<Hosts onHosts={(next) => (hosts = next)} />);
    const view = renderHost();
    expect(hosts?.get(TAB)).toBe(hostElement());
    view.unmount();
    expect(hosts?.has(TAB)).toBe(false);
  });

  it("reports focus moving into the tab", async () => {
    const onFocusIn = vi.fn();
    renderHost({ onFocusIn });
    const field = await fillHost();
    act(() => field.focus());
    expect(onFocusIn).toHaveBeenCalledOnce();
  });

  it("gives the tab's field the keyboard when the pane becomes the focused one", async () => {
    const view = renderHost();
    const field = await fillHost();
    view.update({ active: true });
    expect(document.activeElement).toBe(field);
  });

  it("leaves the keyboard where it is when becoming focused shouldn't take it", async () => {
    const outside = document.body.appendChild(document.createElement("button"));
    outside.focus();
    const view = renderHost({ focusOnActivate: () => false });
    await fillHost();
    view.update({ active: true });
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("holds a focus request until the tab renders in, then gives its field the keyboard", async () => {
    const taken = vi.fn();
    renderHost({ active: true, focusRequest: 1, onFocusRequestTaken: taken });
    expect(taken).not.toHaveBeenCalled();
    const field = await fillHost();
    expect(document.activeElement).toBe(field);
    expect(taken).toHaveBeenCalledExactlyOnceWith(1);
  });

  it("gives the keyboard to a tab that renders in after its pane became the focused one", async () => {
    const view = renderHost();
    view.update({ active: true });
    const field = await fillHost();
    expect(document.activeElement).toBe(field);
  });

  it("takes each focus request once, and focuses the host when the tab has no field", async () => {
    const taken = vi.fn();
    const view = renderHost({ active: true, focusRequest: 1, onFocusRequestTaken: taken });
    await fillHost(document.createElement("p"));
    expect(document.activeElement).toBe(hostElement());
    expect(taken).toHaveBeenCalledExactlyOnceWith(1);
    view.update({ focusRequest: 1 });
    expect(taken).toHaveBeenCalledOnce();
    view.update({ focusRequest: 2 });
    expect(taken).toHaveBeenLastCalledWith(2);
  });

  it("gives the keyboard to a tab the sidebar portals in as the host registers", () => {
    // The sidebar renders the tab into the host it finds in the registry, in the host's commit.
    function Sidebar() {
      const host = useAppPaneHosts(CONVERSATION).get(TAB);
      return host ? createPortal(<input aria-label="tab field" ref={giveBox} />, host) : null;
    }
    const taken = vi.fn();
    render(
      <>
        <Sidebar />
        <AppPaneHost
          conversationId={CONVERSATION}
          tabId={TAB}
          onFocusIn={() => {}}
          active
          focusOnActivate={() => true}
          focusRequest={1}
          onFocusRequestTaken={taken}
        />
      </>,
    );
    expect(document.activeElement).toBe(screen.getByLabelText("tab field"));
    expect(taken).toHaveBeenCalledExactlyOnceWith(1);
  });
});
