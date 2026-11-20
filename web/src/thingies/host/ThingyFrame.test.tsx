import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  THINGY_CONTENT_SECURITY_POLICY,
  ThingyFrame,
  buildThingyFrameDocument,
  selectionPopupPosition,
} from "./ThingyFrame";
import type { Thingy, ThingyContent } from "../api/thingyApi";
import { THINGY_CONNECT_MESSAGE_TYPE, THINGY_HELLO_MESSAGE_TYPE } from "../sdk/thingyProtocol";
import { setOmnigentHostConfig, type ThingyFrameHostProps } from "@/lib/host";

class FakePort {
  postMessage = vi.fn();
  addEventListener = vi.fn();
  removeEventListener = vi.fn();
  start = vi.fn();
  close = vi.fn();
}

class FakeMessageChannel {
  static created: FakeMessageChannel[] = [];
  port1 = new FakePort();
  port2 = new FakePort();
  constructor() {
    FakeMessageChannel.created.push(this);
  }
}

const APP: Thingy = {
  id: "app-1",
  name: "Team board",
  owner_id: "owner@example.com",
  viewer_id: "viewer@example.com",
  viewer_principal_id: "42",
  published_revision: "rev-1",
  version: "v1",
  permissions: { read: true, write_data: true, publish: false, manage: false },
};
const CONTENT: ThingyContent = {
  revision_id: "rev-1",
  html: "<!DOCTYPE html><html><head><title>Board</title></head><body>Hi</body></html>",
  sdk_version: 1,
  capabilities: ["data.read", "unknown.capability"],
};

function renderFrame() {
  const view = render(
    <MemoryRouter>
      <ThingyFrame app={APP} content={CONTENT} onRevisionChanged={() => {}} />
    </MemoryRouter>,
  );
  const frame = () => view.container.querySelector("iframe") as HTMLIFrameElement;
  return { ...view, frame };
}

/** The frame ID and nonce the bootstrap script in the frame's document carries. */
function mountOf(frame: HTMLIFrameElement): { frameId: string; nonce: string } {
  const srcDoc = frame.getAttribute("srcdoc") ?? "";
  const frameId = /"frameId":"([^"]+)"/.exec(srcDoc)?.[1] ?? "";
  const nonce = /"nonce":"([^"]+)"/.exec(srcDoc)?.[1] ?? "";
  return { frameId, nonce };
}

function greet(source: MessageEventSource | null, data: unknown) {
  act(() => {
    window.dispatchEvent(new MessageEvent("message", { data, source }));
  });
}

beforeEach(() => {
  FakeMessageChannel.created = [];
  vi.stubGlobal("MessageChannel", FakeMessageChannel);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildThingyFrameDocument", () => {
  it("puts the CSP and the bootstrap first, after the doctype", () => {
    const document = buildThingyFrameDocument(CONTENT.html, "window.boot = 1;");

    expect(document.startsWith("<!DOCTYPE html><meta http-equiv")).toBe(true);
    expect(document).toContain(`content="${THINGY_CONTENT_SECURITY_POLICY}"`);
    expect(document.indexOf("window.boot = 1;")).toBeLessThan(document.indexOf("<html>"));
    expect(document.match(/<!doctype/gi)).toHaveLength(1);
  });

  it("adds a doctype to HTML without one", () => {
    expect(buildThingyFrameDocument("<p>Hi</p>", "").startsWith("<!DOCTYPE html><meta")).toBe(true);
  });

  it("blocks the network", () => {
    expect(THINGY_CONTENT_SECURITY_POLICY).toContain("connect-src 'none'");
    expect(THINGY_CONTENT_SECURITY_POLICY).toContain("default-src 'none'");
  });
});

describe("selectionPopupPosition", () => {
  const AREA = { left: 100, top: 50, right: 900, bottom: 650 };

  it("centers the popup above the selection", () => {
    expect(selectionPopupPosition(AREA, { x: 300, y: 200, width: 100, height: 20 })).toEqual({
      x: 450,
      y: 250,
    });
  });

  it("keeps a rectangle the frame reports anywhere within the frame's area", () => {
    expect(selectionPopupPosition(AREA, { x: -5_000, y: 99_999, width: 10, height: 10 })).toEqual({
      x: 260,
      y: 650,
    });
    expect(selectionPopupPosition(AREA, { x: 9_000, y: -400, width: 10, height: 10 })).toEqual({
      x: 740,
      y: 90,
    });
  });
});

describe("ThingyFrame", () => {
  afterEach(() => setOmnigentHostConfig({}));

  it("gives a host's own frame the ID its SDK greets with", () => {
    const hostFrame = vi.fn((_props: ThingyFrameHostProps) => <div />);
    setOmnigentHostConfig({ thingyFrame: hostFrame });
    render(
      <MemoryRouter>
        <ThingyFrame app={APP} content={CONTENT} onRevisionChanged={() => {}} />
      </MemoryRouter>,
    );

    const props = hostFrame.mock.calls.at(-1)![0];
    // The host can match its frame to the messages it relays: the bootstrap names this ID.
    expect(props.frameId).toMatch(/^omnigent-thingy-/);
    expect(props.htmlContent).toContain(`"frameId":"${props.frameId}"`);
  });

  it("runs the thingy in a scripts-only sandbox", () => {
    const { frame } = renderFrame();

    expect(frame().getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame().getAttribute("srcdoc")).toContain("Content-Security-Policy");
  });

  it("hands a port to the frame's own window after a hello with this mount's nonce", () => {
    const { frame } = renderFrame();
    const frameWindow = frame().contentWindow as Window;
    const posted = vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
    const { frameId, nonce } = mountOf(frame());

    greet(window, { type: THINGY_HELLO_MESSAGE_TYPE, frameId, nonce });
    greet(frameWindow, { type: THINGY_HELLO_MESSAGE_TYPE, frameId, nonce: "wrong" });
    expect(posted).not.toHaveBeenCalled();

    greet(frameWindow, { type: THINGY_HELLO_MESSAGE_TYPE, frameId, nonce });

    const channel = FakeMessageChannel.created[0];
    expect(posted).toHaveBeenCalledWith(
      { type: THINGY_CONNECT_MESSAGE_TYPE, frameId, nonce },
      "*",
      [channel.port2],
    );
    expect(channel.port1.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "init",
        context: expect.objectContaining({
          app: { id: "app-1", name: "Team board" },
          viewer: { id: "42", displayName: "viewer@example.com" },
          capabilities: ["data.read"],
          permissions: { writeData: true, publish: false },
        }),
      }),
    );

    // One port per document: a second hello gets nothing.
    greet(frameWindow, { type: THINGY_HELLO_MESSAGE_TYPE, frameId, nonce });
    expect(posted).toHaveBeenCalledTimes(1);
  });

  describe("actions", () => {
    beforeEach(() => {
      // The page has transient activation, as after any click in Omnigent.
      Object.defineProperty(navigator, "userActivation", {
        value: { isActive: true },
        configurable: true,
      });
    });
    afterEach(() => {
      Reflect.deleteProperty(navigator, "userActivation");
    });

    function connectFrame() {
      const view = renderFrame();
      const frameWindow = view.frame().contentWindow as Window;
      vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
      greet(frameWindow, { type: THINGY_HELLO_MESSAGE_TYPE, ...mountOf(view.frame()) });
      const port = FakeMessageChannel.created[0].port1;
      const onMessage = port.addEventListener.mock.calls[0][1] as (event: MessageEvent) => void;
      const requestAction = async (id: string) => {
        act(() => {
          onMessage(
            new MessageEvent("message", {
              data: {
                v: 1,
                kind: "request",
                id,
                method: "agent.v1.request",
                params: { action: "run_card", params: {} },
                deadlineMs: 5_000,
              },
            }),
          );
        });
        let response: { error?: { message: string } } | undefined;
        await waitFor(() => {
          response = port.postMessage.mock.calls
            .map(
              ([message]) => message as { kind: string; id: string; error?: { message: string } },
            )
            .find((message) => message.kind === "response" && message.id === id);
          expect(response).toBeDefined();
        });
        return response?.error?.message ?? "";
      };
      return { ...view, requestAction };
    }

    it("refuses an action when the activation came from the host page alone", async () => {
      const { requestAction, getByText } = connectFrame();
      render(<button type="button">Composer</button>);

      fireEvent.pointerDown(getByText("Composer"));

      expect(await requestAction("r1")).toContain("only right after a click in the thingy");
    });

    it("takes one action per click inside the thingy", async () => {
      const { requestAction, frame } = connectFrame();

      act(() => frame().focus());
      // Past the gesture check, the viewer's access decides (this viewer cannot edit the thingy).
      expect(await requestAction("r1")).toContain("needs Can edit thingy access");
      expect(await requestAction("r2")).toContain("only right after a click in the thingy");
    });
  });

  it("gives a reloaded or navigated frame a fresh document and nonce", () => {
    const { frame } = renderFrame();
    const first = mountOf(frame());
    const frameWindow = frame().contentWindow as Window;
    vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
    greet(frameWindow, { type: THINGY_HELLO_MESSAGE_TYPE, ...first });
    fireEvent.load(frame());

    fireEvent.load(frame());

    const channel = FakeMessageChannel.created[0];
    expect(channel.port1.close).toHaveBeenCalled();
    const second = mountOf(frame());
    expect(second.nonce).not.toBe(first.nonce);
    const posted = vi
      .spyOn(frame().contentWindow as Window, "postMessage")
      .mockImplementation(() => {});
    greet(frame().contentWindow, { type: THINGY_HELLO_MESSAGE_TYPE, ...first });
    expect(posted).not.toHaveBeenCalled();
  });
});
