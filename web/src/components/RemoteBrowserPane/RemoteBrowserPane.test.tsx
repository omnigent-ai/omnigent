import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RemoteBrowserPane } from "./RemoteBrowserPane";

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = FakeWebSocket.OPEN;
  binaryType = "blob";
  sent: Record<string, unknown>[] = [];
  readonly url: string;
  private listeners = new Map<string, ((event: unknown) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  send(data: string) {
    this.sent.push(JSON.parse(data));
  }

  close() {
    this.readyState = 3;
  }

  receive(data: unknown) {
    for (const listener of this.listeners.get("message") ?? []) listener({ data });
  }

  receiveJson(message: unknown) {
    this.receive(JSON.stringify(message));
  }

  drop(code: number) {
    this.readyState = 3;
    for (const listener of this.listeners.get("close") ?? []) listener({ code });
  }

  sentOfType(type: string) {
    return this.sent.filter((message) => message.type === type);
  }
}

function frame(seq: number, width: number, height: number): ArrayBuffer {
  const header = new TextEncoder().encode(JSON.stringify({ seq, width, height }));
  const bytes = new Uint8Array(4 + header.length + 2);
  new DataView(bytes.buffer).setUint32(0, header.length);
  bytes.set(header, 4);
  return bytes.buffer;
}

const STATE = {
  type: "state",
  url: "https://example.com/",
  title: "Home",
  can_go_back: true,
  can_go_forward: false,
  loading: false,
  tab_id: "t1",
  tabs: [{ id: "t1", title: "Home", url: "https://example.com/" }],
};

function socket(index = 0): FakeWebSocket {
  return FakeWebSocket.instances[index];
}

async function goLive(readOnly = false) {
  await act(async () => {
    socket().receiveJson({ type: "ready", read_only: readOnly });
    socket().receiveJson(STATE);
  });
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal(
    "createImageBitmap",
    vi.fn(async () => ({ width: 1280, height: 800, close: vi.fn() })),
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({
    left: 10,
    top: 20,
    width: 640,
    height: 400,
  } as DOMRect);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("RemoteBrowserPane", () => {
  it("attaches to the session's stream and shows the page's address once live", async () => {
    render(<RemoteBrowserPane conversationId="conv_1" />);

    expect(socket().url).toContain("/v1/sessions/conv_1/resources/remote_browser/attach");
    expect(screen.getByRole("status")).toHaveTextContent("Connecting to the remote browser…");

    await goLive();

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue(
      "https://example.com/",
    );
    expect(screen.getByRole("button", { name: "Go back" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Go forward" })).toBeDisabled();
  });

  it("acknowledges each frame after drawing it", async () => {
    render(<RemoteBrowserPane conversationId="conv_1" />);
    await goLive();

    await act(async () => {
      socket().receive(frame(7, 1280, 800));
    });

    await waitFor(() =>
      expect(socket().sentOfType("frame_ack")).toEqual([{ type: "frame_ack", seq: 7 }]),
    );
  });

  it("sends presses in the page's own pixels, and typed keys", async () => {
    render(<RemoteBrowserPane conversationId="conv_1" />);
    await goLive();
    await act(async () => {
      socket().receive(frame(1, 1280, 800));
    });
    await waitFor(() => expect(socket().sentOfType("frame_ack")).toHaveLength(1));
    const page = screen.getByRole("application");

    // The canvas is drawn at half the page's size, 10px right and 20px down.
    fireEvent.mouseDown(page, { clientX: 330, clientY: 220, button: 0, buttons: 1 });
    fireEvent.keyDown(page, { key: "a", code: "KeyA", keyCode: 65 });

    expect(socket().sentOfType("mouse")).toEqual([
      expect.objectContaining({ event: "down", x: 640, y: 400, button: "left", buttons: 1 }),
    ]);
    expect(socket().sentOfType("key")).toEqual([
      expect.objectContaining({ event: "down", key: "a", text: "a", key_code: 65 }),
    ]);
  });

  it("navigates to a typed address, adding the scheme", async () => {
    render(<RemoteBrowserPane conversationId="conv_1" />);
    await goLive();
    const address = screen.getByRole("textbox", { name: "Address bar" });

    fireEvent.change(address, { target: { value: "example.org/docs" } });
    fireEvent.keyDown(address, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));

    expect(socket().sentOfType("navigate")).toEqual([
      { type: "navigate", url: "https://example.org/docs" },
    ]);
    expect(socket().sentOfType("reload")).toHaveLength(1);
  });

  it("lets a view-only viewer watch without sending input", async () => {
    render(<RemoteBrowserPane conversationId="conv_1" readOnly />);
    expect(socket().url).toContain("read_only=true");
    await goLive(true);

    expect(screen.getByText("View only")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("img"), { clientX: 330, clientY: 220 });
    expect(screen.getByRole("button", { name: "Reload" })).toBeDisabled();
    expect(socket().sentOfType("mouse")).toEqual([]);
  });

  it("says so and stops dialing when the host has no browser set up", async () => {
    vi.useFakeTimers();
    render(<RemoteBrowserPane conversationId="conv_1" />);

    act(() => socket().drop(4404));
    act(() => {
      vi.advanceTimersByTime(120_000);
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      "No remote browser is set up on this session's host.",
    );
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("dials again after the stream drops", async () => {
    vi.useFakeTimers();
    render(<RemoteBrowserPane conversationId="conv_1" />);

    act(() => socket().drop(4502));
    expect(screen.getByRole("status")).toHaveTextContent("Reconnecting to the remote browser…");
    act(() => {
      vi.advanceTimersByTime(120_000);
    });

    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("holds no connection while it is off screen", () => {
    render(<RemoteBrowserPane conversationId="conv_1" active={false} />);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
