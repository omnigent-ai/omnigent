import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HtmlCommentViewer } from "./HtmlCommentViewer";

// Permissions gate the floating "Add comment" button; default to editable.
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));

afterEach(cleanup);

function renderViewer(content: string, truncated = false) {
  return render(
    <HtmlCommentViewer
      conversationId="conv_1"
      content={content}
      truncated={truncated}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />,
  );
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
