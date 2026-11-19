import { act, fireEvent, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  THINGY_CONTENT_SECURITY_POLICY,
  ThingyFrame,
  buildThingyFrameDocument,
} from "./ThingyFrame";
import type { Thingy, ThingyContent } from "../api/thingyApi";
import { THINGY_CONNECT_MESSAGE_TYPE, THINGY_HELLO_MESSAGE_TYPE } from "../sdk/thingyProtocol";

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

describe("ThingyFrame", () => {
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
