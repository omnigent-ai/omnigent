import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EmbeddedProvider } from "@/lib/embedded";
import { setOmnigentHostConfig, type HtmlPreviewFrameProps } from "@/lib/host";
import { HtmlCommentViewer } from "./HtmlCommentViewer";

// Permissions gate the floating "Add comment" button; default to editable.
vi.mock("@/hooks/usePermissions", () => ({
  useCanEdit: vi.fn(() => true),
}));

beforeEach(() => {
  setOmnigentHostConfig({});
});

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

function HostPreviewFrame({ htmlContent, iframeRef, onLoad }: HtmlPreviewFrameProps) {
  return (
    <iframe
      title="Host preview"
      srcDoc={htmlContent}
      ref={iframeRef}
      onLoad={onLoad}
      sandbox="allow-scripts"
    />
  );
}

describe("HtmlCommentViewer", () => {
  it("warns when a host frame loads without exposing its iframe ref", () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    function HostPreviewWithoutRef({ htmlContent, onLoad }: HtmlPreviewFrameProps) {
      return (
        <iframe
          title="Host preview without ref"
          srcDoc={htmlContent}
          onLoad={onLoad}
          sandbox="allow-scripts"
        />
      );
    }
    setOmnigentHostConfig({ htmlPreviewFrame: HostPreviewWithoutRef });
    renderViewer("<p>Host artifact</p>", false, true);

    fireEvent.load(screen.getByTitle("Host preview without ref"));

    expect(warn).toHaveBeenCalledWith(
      "HTML comment bridge cannot connect: preview frame did not expose its content window.",
    );
    expect(vi.getTimerCount()).toBe(0);
  });

  it("passes prepared HTML and connects the bridge through the host-supplied frame", () => {
    setOmnigentHostConfig({ htmlPreviewFrame: HostPreviewFrame });
    const { rerender } = renderViewer(
      "<html><head></head><body><p>Host artifact</p></body></html>",
      false,
      true,
    );
    const iframe = screen.getByTitle<HTMLIFrameElement>("Host preview");
    expect(screen.queryByTitle("HTML preview")).toBeNull();
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<p>Host artifact</p>");
    expect(srcDoc).toContain('<base target="_blank">');
    expect(srcDoc).toContain("<script data-omni-nonce=");
    expect(srcDoc).toContain("omni-html-comment");
    expect(srcDoc).not.toContain("htmlCommentBridgeRuntime.js");

    if (!iframe.contentWindow) throw new Error("Host preview has no content window");
    const postMessage = vi.spyOn(iframe.contentWindow, "postMessage");
    fireEvent.load(iframe);
    expect(postMessage).toHaveBeenCalledWith(
      { source: "omni-html-comment", nonce: expect.any(String), type: "omni:init" },
      "*",
      [expect.anything()],
    );

    rerender(
      <EmbeddedProvider>
        <HtmlCommentViewer
          conversationId="conv_1"
          content="<p>Updated artifact</p>"
          truncated={false}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={() => {}}
        />
      </EmbeddedProvider>,
    );
    const updatedIframe = screen.getByTitle<HTMLIFrameElement>("Host preview");
    expect(updatedIframe).not.toBe(iframe);
    expect(updatedIframe.getAttribute("srcdoc")).toContain("<p>Updated artifact</p>");
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

  it("injects the bridge inline in standalone mode without a network fetch", () => {
    const { container } = renderViewer("<html><head></head><body><p>doc</p></body></html>");
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain("<script data-omni-nonce=");
    expect(srcDoc).not.toContain("htmlCommentBridgeRuntime.js");
    expect(srcDoc).toContain("omni-html-comment");
    expect(srcDoc).toContain('<base target="_blank">');
  });

  it("preserves the sandboxed srcdoc preview for older embed hosts without a frame component", () => {
    const { container } = renderViewer(
      "<html><head></head><body><p>doc</p></body></html>",
      false,
      true,
    );
    const iframe = container.querySelector('iframe[title="HTML preview"]') as HTMLIFrameElement;
    const srcDoc = iframe.getAttribute("srcdoc") ?? "";
    expect(iframe.getAttribute("sandbox")).toContain("allow-scripts");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
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
