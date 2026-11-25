import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  GADGET_CONTENT_SECURITY_POLICY,
  GadgetFrame,
  buildGadgetFrameDocument,
  selectionPopupPosition,
} from "./GadgetFrame";
import type { Gadget, GadgetContent } from "../api/gadgetApi";
import { GADGET_CONNECT_MESSAGE_TYPE, GADGET_HELLO_MESSAGE_TYPE } from "../sdk/gadgetProtocol";
import { setOmnigentHostConfig, type GadgetFrameHostProps } from "@/lib/host";
import { resetGadgetRailRequests, useGadgetRailRequest } from "@/lib/gadgetRailTabs";
import { forgetGadgetSessionStarts, setGadgetSessionStarting } from "./gadgetSession";

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

const APP: Gadget = {
  id: "app-1",
  name: "Team board",
  owner_id: "owner@example.com",
  viewer_id: "viewer@example.com",
  viewer_principal_id: "42",
  published_revision: "rev-1",
  version: "v1",
  permissions: { read: true, write_data: true, publish: false, manage: false },
};
const CONTENT: GadgetContent = {
  revision_id: "rev-1",
  html: "<!DOCTYPE html><html><head><title>Board</title></head><body>Hi</body></html>",
  sdk_version: 1,
  capabilities: ["data.read", "unknown.capability"],
};

function renderFrame() {
  const view = render(
    <MemoryRouter>
      <GadgetFrame app={APP} content={CONTENT} onRevisionChanged={() => {}} />
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
  resetGadgetRailRequests();
});

describe("buildGadgetFrameDocument", () => {
  it("puts the CSP and the bootstrap first, after the doctype", () => {
    const document = buildGadgetFrameDocument(CONTENT.html, "window.boot = 1;");

    expect(document.startsWith("<!DOCTYPE html><meta http-equiv")).toBe(true);
    expect(document).toContain(`content="${GADGET_CONTENT_SECURITY_POLICY}"`);
    expect(document.indexOf("window.boot = 1;")).toBeLessThan(document.indexOf("<html>"));
    expect(document.match(/<!doctype/gi)).toHaveLength(1);
  });

  it("adds a doctype to HTML without one", () => {
    expect(buildGadgetFrameDocument("<p>Hi</p>", "").startsWith("<!DOCTYPE html><meta")).toBe(true);
  });

  it("sets a content security policy that lists no network sources", () => {
    expect(GADGET_CONTENT_SECURITY_POLICY).toContain("connect-src 'none'");
    expect(GADGET_CONTENT_SECURITY_POLICY).toContain("default-src 'none'");
    // Not a directive every browser knows: it would only add a console warning on each load.
    expect(GADGET_CONTENT_SECURITY_POLICY).not.toContain("webrtc");
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

describe("GadgetFrame", () => {
  afterEach(() => setOmnigentHostConfig({}));

  it("gives a host's own frame the ID its SDK greets with", () => {
    const hostFrame = vi.fn((_props: GadgetFrameHostProps) => <div />);
    setOmnigentHostConfig({ gadgetFrame: hostFrame });
    render(
      <MemoryRouter>
        <GadgetFrame app={APP} content={CONTENT} onRevisionChanged={() => {}} />
      </MemoryRouter>,
    );

    const props = hostFrame.mock.calls.at(-1)![0];
    // The host can match its frame to the messages it relays: the bootstrap names this ID.
    expect(props.frameId).toMatch(/^omnigent-gadget-/);
    expect(props.htmlContent).toContain(`"frameId":"${props.frameId}"`);
  });

  it("runs the gadget in a scripts-only sandbox", () => {
    const { frame } = renderFrame();

    expect(frame().getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame().getAttribute("srcdoc")).toContain("Content-Security-Policy");
  });

  it("hands a port to the frame's own window after a hello with this mount's nonce", () => {
    const { frame } = renderFrame();
    const frameWindow = frame().contentWindow as Window;
    const posted = vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
    const { frameId, nonce } = mountOf(frame());

    greet(window, { type: GADGET_HELLO_MESSAGE_TYPE, frameId, nonce });
    greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, frameId, nonce: "wrong" });
    expect(posted).not.toHaveBeenCalled();

    expect(useGadgetRailRequest.getState().frameConnections["app-1"]).toBeUndefined();
    greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, frameId, nonce });
    // The gadget beside New session waits for this to hand its full screen to the session.
    expect(useGadgetRailRequest.getState().frameConnections["app-1"]).toBe(1);

    const channel = FakeMessageChannel.created[0];
    expect(posted).toHaveBeenCalledWith(
      { type: GADGET_CONNECT_MESSAGE_TYPE, frameId, nonce },
      "*",
      [channel.port2],
    );
    expect(channel.port1.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "init",
        context: expect.objectContaining({
          app: { id: "app-1", name: "Team board" },
          // Never the viewer's login: an older server's principal ID, and no name.
          viewer: { id: "42", displayName: null },
          capabilities: ["data.read"],
          permissions: { writeData: true, publish: false },
        }),
      }),
    );

    // One port per document: a second hello gets nothing.
    greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, frameId, nonce });
    expect(posted).toHaveBeenCalledTimes(1);
  });

  it("tells the frame its gadget session is starting, at connect and as it changes", () => {
    setGadgetSessionStarting("app-1", true);
    try {
      const { frame } = renderFrame();
      const frameWindow = frame().contentWindow as Window;
      vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
      const { frameId, nonce } = mountOf(frame());
      greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, frameId, nonce });
      const port = FakeMessageChannel.created[0].port1;
      const events = () =>
        port.postMessage.mock.calls
          .map(([message]) => message as { kind?: string; starting?: boolean })
          .filter((message) => message.kind === "event");

      expect(events()).toEqual([{ v: 1, kind: "event", event: "agent.starting", starting: true }]);
      // Another gadget's session doesn't concern this frame.
      act(() => setGadgetSessionStarting("app-2", true));
      expect(events()).toHaveLength(1);
      act(() => setGadgetSessionStarting("app-1", false));
      expect(events().at(-1)).toEqual({
        v: 1,
        kind: "event",
        event: "agent.starting",
        starting: false,
      });
    } finally {
      forgetGadgetSessionStarts();
    }
  });

  it("tells gadget code only the gadget-scoped viewer ID and a real name, never the login", () => {
    const view = render(
      <MemoryRouter>
        <GadgetFrame
          app={APP}
          content={{ ...CONTENT, viewer: { id: "u_opaque", display_name: "Ada" } }}
          onRevisionChanged={() => {}}
        />
      </MemoryRouter>,
    );
    const frame = view.container.querySelector("iframe") as HTMLIFrameElement;
    const frameWindow = frame.contentWindow as Window;
    vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
    const { frameId, nonce } = mountOf(frame);
    greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, frameId, nonce });
    const channel = FakeMessageChannel.created.at(-1) as FakeMessageChannel;
    const init = (channel.port1.postMessage as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(init.context.viewer).toEqual({ id: "u_opaque", displayName: "Ada" });
    expect(JSON.stringify(init)).not.toContain("viewer@example.com");
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
      greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, ...mountOf(view.frame()) });
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

      expect(await requestAction("r1")).toContain("only right after a click in the gadget");
    });

    it("takes one action per click inside the gadget", async () => {
      const { requestAction, frame } = connectFrame();

      act(() => frame().focus());
      // Past the gesture check, the viewer's access decides (this viewer cannot edit the gadget).
      expect(await requestAction("r1")).toContain("needs Can edit gadget access");
      expect(await requestAction("r2")).toContain("only right after a click in the gadget");
    });
  });

  describe("pages the gadget opens", () => {
    beforeEach(() => {
      Object.defineProperty(navigator, "userActivation", {
        value: { isActive: true },
        configurable: true,
      });
    });
    afterEach(() => {
      Reflect.deleteProperty(navigator, "userActivation");
      delete window.__OMNIGENT_BASE_PATH__;
    });

    function connectAndClick() {
      const view = renderFrame();
      const frameWindow = view.frame().contentWindow as Window;
      vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
      greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, ...mountOf(view.frame()) });
      const port = FakeMessageChannel.created[0].port1;
      const onMessage = port.addEventListener.mock.calls[0][1] as (event: MessageEvent) => void;
      // A click inside the gadget lets it open one page.
      act(() => view.frame().focus());
      const call = (method: string, params: Record<string, unknown>) =>
        act(() => {
          onMessage(
            new MessageEvent("message", {
              data: { v: 1, kind: "request", id: "n1", method, params, deadlineMs: 5_000 },
            }),
          );
        });
      return { ...view, call };
    }

    it("shows the gadget beside a session it opens in the panel, without sending it actions", async () => {
      const fetchMock = vi.fn(
        async () =>
          new Response(JSON.stringify({ items: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      );
      vi.stubGlobal("fetch", fetchMock);
      const { call } = connectAndClick();

      call("navigation.v1.openSession", { sessionId: "s2", display: "panel" });

      // Linked to the session first, so its side panel lists the gadget to show.
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(String(url)).toContain("/v1/gadgets/app-1/sessions/s2");
      expect(init.method).toBe("PUT");
      expect(JSON.parse(String(init.body))).toEqual({ linked: true, handles_actions: false });
      await waitFor(() =>
        expect(useGadgetRailRequest.getState().requested).toMatchObject({
          conversationId: "s2",
          key: "gadget:app-1",
        }),
      );
    });

    it("hands a message it writes for a new session to the Create session menu to show first", () => {
      act(() => useGadgetRailRequest.setState({ fullScreenGadgetId: "app-1" }));
      const { call } = connectAndClick();

      call("navigation.v1.newSession", { prompt: "Rewrite every card", display: "panel" });

      const state = useGadgetRailRequest.getState();
      expect(state.createSessionMenuRequested).toBe(true);
      expect(state.createSessionReview).toMatchObject({
        gadgetId: "app-1",
        gadgetName: "Team board",
      });
      // The menu shows the whole message Create session would send.
      expect(state.createSessionReview?.prompt).toContain("Rewrite every card");
    });

    it("asks for no review when the gadget's new session carries no message", () => {
      act(() => useGadgetRailRequest.setState({ fullScreenGadgetId: "app-1" }));
      const { call } = connectAndClick();

      call("navigation.v1.newSession", { display: "panel" });

      expect(useGadgetRailRequest.getState().createSessionMenuRequested).toBe(true);
      expect(useGadgetRailRequest.getState().createSessionReview).toBeNull();
    });

    it("opens a page in a new tab under the deployment's base path", () => {
      window.__OMNIGENT_BASE_PATH__ = "/app";
      const opened = vi.spyOn(window, "open").mockImplementation(() => null);
      const { call } = connectAndClick();

      call("navigation.v1.openSession", { sessionId: "s2", display: "tab" });

      expect(opened).toHaveBeenCalledWith(
        `${window.location.origin}/app/c/s2`,
        "_blank",
        "noopener,noreferrer",
      );
    });
  });

  describe("the viewer's sessions", () => {
    function connectWithSessions() {
      const view = render(
        <MemoryRouter>
          <GadgetFrame
            app={APP}
            content={{ ...CONTENT, capabilities: ["data.read", "sessions.read"] }}
            onRevisionChanged={() => {}}
          />
        </MemoryRouter>,
      );
      const frame = view.container.querySelector("iframe") as HTMLIFrameElement;
      const frameWindow = frame.contentWindow as Window;
      vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
      greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, ...mountOf(frame) });
      const port = FakeMessageChannel.created[0].port1;
      const onMessage = port.addEventListener.mock.calls[0][1] as (event: MessageEvent) => void;
      const list = (id: string) =>
        act(() => {
          onMessage(
            new MessageEvent("message", {
              data: {
                v: 1,
                kind: "request",
                id,
                method: "sessions.v1.list",
                params: {},
                deadlineMs: 5_000,
              },
            }),
          );
        });
      const responseFor = (id: string) =>
        port.postMessage.mock.calls
          .map(([message]) => message as { kind: string; id: string; ok?: boolean })
          .find((message) => message.kind === "response" && message.id === id);
      return { list, responseFor };
    }

    it("lists them only after the viewer allows it, asking once per page", async () => {
      const fetchMock = vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              data: [{ id: "s1", title: "Ship it", status: "idle" }],
              has_more: false,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      );
      vi.stubGlobal("fetch", fetchMock);
      const { list, responseFor } = connectWithSessions();

      list("r1");
      expect(await screen.findByText("Let this gadget see your sessions?")).toBeVisible();
      // Nothing is read before the viewer answers.
      expect(fetchMock).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Allow" }));
      await waitFor(() => expect(responseFor("r1")).toMatchObject({ ok: true }));
      const [url] = fetchMock.mock.calls[0] as unknown as [string];
      expect(String(url)).toContain("/v1/sessions?");

      // Allowed once, a later list in the same page does not ask again.
      list("r2");
      await waitFor(() => expect(responseFor("r2")).toMatchObject({ ok: true }));
      expect(screen.queryByText("Let this gadget see your sessions?")).toBeNull();
    });

    it("asks again after a frame it stopped for navigating is reloaded", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(
          async () =>
            new Response(JSON.stringify({ data: [], has_more: false }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
        ),
      );
      const { list, responseFor } = connectWithSessions();
      list("r1");
      fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
      await waitFor(() => expect(responseFor("r1")).toMatchObject({ ok: true }));

      const frame = () => document.querySelector("iframe") as HTMLIFrameElement;
      // Loads until the gadget's document leaves its page and the frame is stopped.
      while (frame() !== null) fireEvent.load(frame());
      fireEvent.click(screen.getByRole("button", { name: "Reload gadget" }));
      const frameWindow = frame().contentWindow as Window;
      vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
      greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, ...mountOf(frame()) });
      const port = FakeMessageChannel.created.at(-1)!.port1;
      const onMessage = port.addEventListener.mock.calls[0][1] as (event: MessageEvent) => void;
      act(() => {
        onMessage(
          new MessageEvent("message", {
            data: {
              v: 1,
              kind: "request",
              id: "r2",
              method: "sessions.v1.list",
              params: {},
              deadlineMs: 5_000,
            },
          }),
        );
      });
      expect(await screen.findByText("Let this gadget see your sessions?")).toBeVisible();
    });

    it("refuses the list, and reads nothing, when the viewer does not allow it", async () => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const { list, responseFor } = connectWithSessions();

      list("r1");
      fireEvent.click(await screen.findByRole("button", { name: "Don’t allow" }));
      await waitFor(() =>
        expect(responseFor("r1")).toMatchObject({
          ok: false,
          error: { code: "PERMISSION_DENIED" },
        }),
      );
      list("r2");
      await waitFor(() => expect(responseFor("r2")).toMatchObject({ ok: false }));
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  // Teardown after the navigation, not prevention: its request has already left by then.
  it("tears down a frame that navigated and says so, then reloads it on request", () => {
    const { frame } = renderFrame();
    const first = mountOf(frame());
    const frameWindow = frame().contentWindow as Window;
    vi.spyOn(frameWindow, "postMessage").mockImplementation(() => {});
    greet(frameWindow, { type: GADGET_HELLO_MESSAGE_TYPE, ...first });
    fireEvent.load(frame());

    // A second load is the gadget's document reloading or leaving its page.
    fireEvent.load(frame());

    const channel = FakeMessageChannel.created[0];
    expect(channel.port1.close).toHaveBeenCalled();
    expect(document.querySelector("iframe")).toBeNull();
    expect(
      screen.getByText("This gadget tried to leave its page, so Omnigent stopped it"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reload gadget" }));
    const second = mountOf(frame());
    expect(second.nonce).not.toBe(first.nonce);
    const posted = vi
      .spyOn(frame().contentWindow as Window, "postMessage")
      .mockImplementation(() => {});
    // The old document's hello can't take over the new mount.
    greet(frame().contentWindow, { type: GADGET_HELLO_MESSAGE_TYPE, ...first });
    expect(posted).not.toHaveBeenCalled();
  });
});
