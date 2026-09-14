import { act } from "react";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HtmlCommentViewer } from "./HtmlCommentViewer";
import { BRIDGE_MSG, BRIDGE_SOURCE } from "./htmlCommentBridge";

// Permissions gate the floating "Add comment" button; default to editable.
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));

let inbound: ((event: MessageEvent) => void) | null;

beforeEach(() => {
  inbound = null;
  vi.stubGlobal(
    "MessageChannel",
    class {
      port1 = {
        onmessage: null as ((event: MessageEvent) => void) | null,
        postMessage: vi.fn(),
        close: vi.fn(),
      };
      port2 = { close: vi.fn() };

      constructor() {
        inbound = (event) => this.port1.onmessage?.(event);
      }
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

function renderViewer(content: string, truncated = false, readOnly = false) {
  return render(
    <HtmlCommentViewer
      conversationId="conv_1"
      readOnly={readOnly}
      content={content}
      truncated={truncated}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />,
  );
}

describe("HtmlCommentViewer", () => {
  async function selectPreviewText(readOnly: boolean) {
    const { container } = renderViewer("<body><p>doc</p></body>", false, readOnly);
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    const postMessage = vi.spyOn(iframe.contentWindow!, "postMessage").mockImplementation(() => {});
    fireEvent.load(iframe);
    const init = postMessage.mock.calls[0][0] as { nonce: string };
    await act(async () => {
      inbound?.({
        data: {
          source: BRIDGE_SOURCE,
          nonce: init.nonce,
          type: BRIDGE_MSG.selection,
          text: "doc",
          occ: 0,
          rect: { left: 10, top: 10, right: 30, bottom: 20 },
        },
      } as MessageEvent);
    });
  }

  it("offers comments after a selection for an editable session", async () => {
    await selectPreviewText(false);
    expect(document.querySelector("[data-add-comment-btn]")).not.toBeNull();
  });

  it("offers no comment control after a selection when explicitly read-only", async () => {
    await selectPreviewText(true);
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });

  it("renders the preview in a sandboxed iframe that still withholds allow-same-origin", () => {
    const { container } = renderViewer("<html><body><p>doc</p></body></html>");
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    expect(iframe).not.toBeNull();
    const sandbox = iframe.getAttribute("sandbox") ?? "";
    expect(sandbox).toContain("allow-scripts");
    // The security-critical invariant: the opaque origin must be preserved so
    // untrusted artifact HTML can never reach the host app.
    expect(sandbox).not.toContain("allow-same-origin");
  });

  it("injects the comment bridge (and base-target) into the iframe srcDoc", () => {
    const { container } = renderViewer("<html><head></head><body><p>doc</p></body></html>");
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<script>");
    expect(srcDoc).toContain("omni-html-comment");
    expect(srcDoc).toContain('<base target="_blank">');
  });

  it("shows the truncated banner only when truncated", () => {
    const { queryByText, rerender } = renderViewer("<body>x</body>", false);
    expect(queryByText(/truncated/i)).toBeNull();
    rerender(
      <HtmlCommentViewer
        conversationId="conv_1"
        content="<body>x</body>"
        truncated={true}
        comments={[]}
        activeSelection={null}
        onSetActiveSelection={() => {}}
      />,
    );
    expect(queryByText(/truncated/i)).not.toBeNull();
  });

  it("does not show the floating Add-comment button before any selection", () => {
    renderViewer("<body><p>doc</p></body>");
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });
});
