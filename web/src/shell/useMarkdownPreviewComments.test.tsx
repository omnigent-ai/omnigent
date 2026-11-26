import { render } from "@testing-library/react";
import { useRef } from "react";
import ReactMarkdown from "react-markdown";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { rehypeSourceOffsets } from "./markdownPreviewSource";
import { useMarkdownPreviewComments } from "./useMarkdownPreviewComments";

// jsdom has no CSS Custom Highlight API; a Set of ranges stands in for Highlight.
class FakeHighlight extends Set<Range> {}
let registry: Map<string, FakeHighlight>;

beforeEach(() => {
  registry = new Map();
  vi.stubGlobal("Highlight", FakeHighlight);
  vi.stubGlobal("CSS", { highlights: registry });
});
afterEach(() => vi.unstubAllGlobals());

function comment(source: string, text: string, id: string): Comment {
  const start = source.indexOf(text);
  return {
    id,
    conversation_id: "conv",
    path: "doc.md",
    start_index: start,
    end_index: start + text.length,
    body: "note",
    status: "draft",
    created_at: 0,
    anchor_content: text,
  } as Comment;
}

function Preview(props: {
  content: string;
  comments: Comment[];
  enabled?: boolean;
  activeSelection?: ActiveSelection | null;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  useMarkdownPreviewComments({
    rootRef,
    content: props.content,
    enabled: props.enabled ?? true,
    canEdit: true,
    comments: props.comments,
    activeSelection: props.activeSelection ?? null,
    onSetActiveSelection: () => {},
  });
  return (
    <div ref={rootRef} data-testid="preview">
      <ReactMarkdown rehypePlugins={[rehypeSourceOffsets]}>{props.content}</ReactMarkdown>
    </div>
  );
}

const painted = (name: string) =>
  [...(registry.get(name) ?? [])].map((range) => range.toString()).sort();

describe("useMarkdownPreviewComments highlights", () => {
  it("keeps two open previews' highlights apart, and unmounting one leaves the other", () => {
    const first = "Alpha beta gamma.\n";
    const second = "Delta epsilon zeta.\n";
    const left = render(<Preview content={first} comments={[comment(first, "beta", "c1")]} />);
    render(<Preview content={second} comments={[comment(second, "epsilon", "c2")]} />);
    expect(painted("md-preview-comment")).toEqual(["beta", "epsilon"]);

    left.unmount();
    expect(painted("md-preview-comment")).toEqual(["epsilon"]);
  });

  it("clears its highlights when commenting turns off while mounted", () => {
    const source = "Alpha beta gamma.\n";
    const comments = [comment(source, "beta", "c1")];
    const view = render(<Preview content={source} comments={comments} />);
    expect(painted("md-preview-comment")).toEqual(["beta"]);

    view.rerender(<Preview content={source} comments={comments} enabled={false} />);
    expect(painted("md-preview-comment")).toEqual([]);
  });

  it("moves the active highlight without re-reading the rendered runs", () => {
    const source = "Alpha beta gamma.\n";
    const comments = [comment(source, "beta", "c1"), comment(source, "gamma", "c2")];
    const view = render(<Preview content={source} comments={comments} />);
    const scans = vi.spyOn(Element.prototype, "querySelectorAll");

    for (const target of comments) {
      view.rerender(
        <Preview
          content={source}
          comments={comments}
          activeSelection={{
            start_index: target.start_index,
            end_index: target.end_index,
            anchor_content: target.anchor_content ?? "",
          }}
        />,
      );
    }
    expect(painted("md-preview-comment-active")).toEqual(["gamma"]);
    expect(painted("md-preview-comment")).toEqual(["beta", "gamma"]);
    expect(scans).not.toHaveBeenCalled();
  });
});
