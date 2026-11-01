/** Connect a test to a mounted HTML preview's comment bridge. */
import { waitFor } from "@testing-library/react";
import { expect, vi } from "vitest";
import { BRIDGE_SOURCE } from "@/shell/htmlCommentBridge";

/**
 * Wait for the viewer's bridge handshake and return the frame's end of the port.
 *
 * @param iframe The mounted preview frame.
 * @returns The port plus `send`, which posts a message carrying the bridge's source and nonce.
 */
export async function connectHtmlPreviewBridge(iframe: HTMLIFrameElement) {
  if (!iframe.contentWindow) throw new Error("Preview has no content window");
  const postMessage = vi.spyOn(iframe.contentWindow, "postMessage");
  // jsdom loads the srcdoc itself; a synthetic load would be superseded by it.
  await waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1));
  const [init, , transfer] = postMessage.mock.calls[0] as unknown as [
    { nonce: string },
    string,
    MessagePort[],
  ];
  const port = transfer[0];
  const send = (msg: Record<string, unknown>) =>
    port.postMessage({ source: BRIDGE_SOURCE, nonce: init.nonce, ...msg });
  return { port, send };
}
