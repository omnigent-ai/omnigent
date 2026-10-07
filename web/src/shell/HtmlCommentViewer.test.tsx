import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EmbeddedProvider } from "@/lib/embedded";
import { HtmlCommentViewer } from "./HtmlCommentViewer";

// Permissions gate the floating "Add comment" button; default to editable.
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));
vi.mock("@/lib/host", () => ({ getEmbedRoot: vi.fn(() => null) }));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function renderViewer(content: string, truncated = false, embedded = false) {
  const viewer = (
    <HtmlCommentViewer
      conversationId="conv_1"
      content={content}
      truncated={truncated}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />
  );
  return render(embedded ? <EmbeddedProvider>{viewer}</EmbeddedProvider> : viewer);
}

describe("HtmlCommentViewer", () => {
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

  it("injects the bridge inline in standalone mode without a network fetch", () => {
    const { container } = renderViewer("<html><head></head><body><p>doc</p></body></html>");
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<script data-omni-nonce=");
    expect(srcDoc).not.toContain("htmlCommentBridgeRuntime.js");
    expect(srcDoc).toContain("omni-html-comment");
    expect(srcDoc).toContain('<base target="_blank">');
  });

  it("loads the static bridge runtime externally in embed mode", () => {
    const { container } = renderViewer(
      "<html><head></head><body><p>doc</p></body></html>",
      false,
      true,
    );
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<script src=");
    expect(srcDoc).toContain("htmlCommentBridgeRuntime.js");
    expect(srcDoc).toContain("data-omni-nonce=");
    expect(srcDoc).not.toContain("new Function");
  });

  it("starts the diagnostic timer only after the iframe loads", () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { container } = renderViewer("<body><p>doc</p></body>");
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;

    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(5_000));
    expect(warn).not.toHaveBeenCalled();

    act(() => iframe.dispatchEvent(new Event("load")));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    act(() => vi.advanceTimersByTime(5_000));

    expect(warn).toHaveBeenCalledWith(
      "HTML comment bridge did not become ready; comments are unavailable.",
    );
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).not.toContain("htmlCommentBridgeRuntime.js");
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
