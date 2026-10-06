import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";
import { BRIDGE_MSG, BRIDGE_SOURCE, injectCommentBridge } from "./htmlCommentBridge";
import { useHtmlCommentBridge } from "./useHtmlCommentBridge";

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));

const NONCE = "hook-bridge-nonce";
vi.mock("@/lib/randomUUID", () => ({ randomUUID: () => NONCE }));

afterEach(cleanup);

const SOURCE = `<html><body><p id="a">unique hook anchor sentence</p></body></html>`;
const noopSetActiveSelection = () => {};

function Host({
  scale = 1,
  onSetActiveSelection = noopSetActiveSelection,
}: {
  scale?: number;
  onSetActiveSelection?: (
    sel: {
      start_index: number;
      end_index: number;
      anchor_content: string;
    } | null,
  ) => void;
}) {
  const { nonce, iframeRef, addCommentPortal } = useHtmlCommentBridge({
    conversationId: "conv_1",
    content: SOURCE,
    docKey: SOURCE,
    comments: [],
    activeSelection: null,
    onSetActiveSelection,
    scale,
  });
  return (
    <>
      <iframe
        ref={iframeRef}
        title="hook-preview"
        sandbox={HTML_PREVIEW_SANDBOX}
        srcDoc={injectCommentBridge(SOURCE, nonce)}
      />
      {addCommentPortal}
    </>
  );
}

async function connectBridge(iframe: HTMLIFrameElement): Promise<MessagePort> {
  // The hook listens for load and transfers port2; capture it before load so we
  // can speak on the channel as the iframe would.
  let port2: MessagePort | null = null;
  const win = {
    postMessage: (_msg: unknown, _origin: string, transfer?: Transferable[]) => {
      port2 = (transfer?.[0] as MessagePort) ?? null;
    },
  };
  Object.defineProperty(iframe, "contentWindow", { configurable: true, value: win });
  await act(async () => {
    fireEvent.load(iframe);
  });
  expect(port2).not.toBeNull();
  return port2!;
}

describe("useHtmlCommentBridge", () => {
  it("maps a selection message to onSetActiveSelection with source offsets", async () => {
    const onSet = vi.fn();
    render(<Host onSetActiveSelection={onSet} />);
    const iframe = screen.getByTitle("hook-preview") as HTMLIFrameElement;
    vi.spyOn(iframe, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const port = await connectBridge(iframe);
    const text = "unique hook anchor sentence";
    await act(async () => {
      port.postMessage({
        source: BRIDGE_SOURCE,
        nonce: NONCE,
        type: BRIDGE_MSG.selection,
        text,
        occ: 0,
        rect: { left: 10, top: 20, right: 40, bottom: 30 },
      });
    });
    await waitFor(() => {
      expect(document.querySelector("[data-add-comment-btn]")).not.toBeNull();
    });
    fireEvent.click(document.querySelector("[data-add-comment-btn]")!);
    const at = SOURCE.indexOf(text);
    expect(onSet).toHaveBeenCalledWith({
      start_index: at,
      end_index: at + text.length,
      anchor_content: text,
    });
  });

  it("positions the floating button using the stage scale", async () => {
    render(<Host scale={0.5} />);
    const iframe = screen.getByTitle("hook-preview") as HTMLIFrameElement;
    vi.spyOn(iframe, "getBoundingClientRect").mockReturnValue({
      left: 200,
      top: 100,
      right: 300,
      bottom: 200,
      width: 100,
      height: 100,
      x: 200,
      y: 100,
      toJSON: () => ({}),
    });
    const port = await connectBridge(iframe);
    await act(async () => {
      port.postMessage({
        source: BRIDGE_SOURCE,
        nonce: NONCE,
        type: BRIDGE_MSG.selection,
        text: "unique hook anchor sentence",
        occ: 0,
        rect: { left: 40, top: 80, right: 60, bottom: 90 },
      });
    });
    await waitFor(() => {
      expect(document.querySelector("[data-add-comment-btn]")).not.toBeNull();
    });
    const btn = document.querySelector("[data-add-comment-btn]") as HTMLButtonElement;
    expect(btn.style.left).toBe(`${200 + 40 * 0.5}px`);
    expect(btn.style.top).toBe(`${100 + 80 * 0.5 - 6}px`);
  });
});
