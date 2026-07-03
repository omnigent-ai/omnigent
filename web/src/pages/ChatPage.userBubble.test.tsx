import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { BubbleView } from "./ChatPage";

// UserBubble renders its text through the same markdown renderer as the
// assistant bubble (FilePathAwareMessageResponse → Streamdown). These tests
// pin that wiring: if the text path reverts to a raw `{text}` string, the
// markdown syntax would render literally and these assertions would fail.

afterEach(cleanup);

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};

function userBubble(text: string, overrides: Partial<Extract<Bubble, { kind: "user" }>> = {}) {
  return {
    kind: "user" as const,
    itemId: "u1",
    content: [{ type: "input_text" as const, text }],
    ...overrides,
  };
}

function assistantBubble(
  lifecycle: Extract<Bubble, { kind: "assistant" }>["lifecycle"],
  text = "partial answer",
): Extract<Bubble, { kind: "assistant" }> {
  return {
    kind: "assistant",
    responseId: "codex_turn_123",
    stableId: "msg_1",
    lifecycle,
    error: null,
    items: [{ kind: "text", itemId: "msg_1", text, final: true }],
  };
}

function renderBubble(bubble: Bubble) {
  return render(
    <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
      <BubbleView bubble={bubble} />
    </FileViewerContext.Provider>,
  );
}

describe("UserBubble markdown rendering", () => {
  it("renders **bold** markdown as a strong node, not literal asterisks", () => {
    renderBubble(userBubble("hello **world**"));
    // Streamdown emits bold as an element tagged data-streamdown="strong"
    // (a <span class="font-semibold">, not a literal <strong>). Finding it
    // proves the inline markdown parser ran; a raw-text path would have no
    // such node.
    const bolded = screen.getByText("world");
    expect(bolded.getAttribute("data-streamdown")).toBe("strong");
    // The literal markdown source must NOT survive as text.
    expect(screen.queryByText(/\*\*world\*\*/)).toBeNull();
  });

  it("renders a markdown list as <li> items", async () => {
    renderBubble(userBubble("- first\n- second"));
    // Two list items prove the markdown block parser ran. A raw-text path
    // would render the source as a single line with literal hyphens.
    const first = await screen.findByText("first", { selector: "li, li *" });
    const second = await screen.findByText("second", { selector: "li, li *" });
    expect(first.closest("li")).not.toBeNull();
    expect(second.closest("li")).not.toBeNull();
  });

  it("renders fenced code blocks inside a <pre> wrapper", async () => {
    renderBubble(userBubble("```python\ndef foo():\n    return 1\n```\n"));
    // Mirrors the assistant-side guarantee: fenced code keeps its <pre>
    // wrapper rather than collapsing to inline text.
    const pre = await screen.findByText(/def foo/, { selector: "pre, pre *" });
    expect(pre.closest("pre")).not.toBeNull();
  });

  it("keeps single newlines as <br> line breaks (remark-breaks)", () => {
    const { container } = renderBubble(userBubble("line one\nline two"));
    // The `breaks` prop appends remark-breaks, so a single newline becomes a
    // hard <br>. Without it, CommonMark would collapse the newline to a space
    // and this query would find no <br>. Both lines live in one paragraph.
    expect(container.querySelectorAll("br").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/line one/)).toBeDefined();
    expect(screen.getByText(/line two/)).toBeDefined();
  });

  it("still renders GFM tables — remark-breaks extends, not replaces, the defaults", async () => {
    renderBubble(userBubble("| a | b |\n| - | - |\n| 1 | 2 |"));
    // The regression guard for the extend-not-replace decision: if we had
    // passed [remarkBreaks] alone, Streamdown would drop remark-gfm and this
    // table would render as literal pipe text with no <table>/<td>.
    const cell = await screen.findByText("1", { selector: "td, td *" });
    expect(cell.closest("table")).not.toBeNull();
  });
});

describe("AssistantBubble lifecycle rendering", () => {
  it("shows an interrupted indicator for cancelled assistant bubbles", () => {
    renderBubble(assistantBubble("cancelled"));

    expect(screen.getByTestId("assistant-interrupted-indicator")).toHaveTextContent("Interrupted");
  });

  it("does not show an interrupted indicator for completed assistant bubbles", () => {
    renderBubble(assistantBubble("completed"));

    expect(screen.queryByTestId("assistant-interrupted-indicator")).toBeNull();
  });
});

describe("UserBubble copy button", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("copies the message text to the clipboard when clicked", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    renderBubble(userBubble("copy me please"));

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("copy me please"));
  });

  it("does not render a copy button for an attachments-only message (no text)", () => {
    renderBubble(
      userBubble("", {
        content: [{ type: "input_image", file_id: "f1", filename: "a.png" }],
      }),
    );
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});

describe("UserBubble @-mention attachment chips", () => {
  it("shows file and folder chips from [Attached: …] markers and hides the markers", () => {
    renderBubble(userBubble("[Attached: src/server.ts]\n[Attached: docs/]\n\nsummarize these"));

    // The marker paths surface as chips (folder keeps its trailing slash)...
    expect(screen.getByText("@src/server.ts")).toBeInTheDocument();
    expect(screen.getByText("@docs/")).toBeInTheDocument();
    // ...while the raw "[Attached: …]" marker text is stripped from the body.
    expect(screen.queryByText(/\[Attached:/)).toBeNull();
    expect(screen.getByText("summarize these")).toBeInTheDocument();
  });

  it("renders chips for the codex 'Attached file:' wording too", () => {
    renderBubble(userBubble("[Attached file: src/a.ts]\n\ncheck this"));
    expect(screen.getByText("@src/a.ts")).toBeInTheDocument();
    expect(screen.queryByText(/\[Attached/)).toBeNull();
  });

  it("shows the line span of a partial-file attach in its own (non-truncating) node", () => {
    renderBubble(userBubble("[Attached: bob-max-gain/docker-compose.yml:2-9]\n\nreview"));
    expect(screen.getByText("@bob-max-gain/docker-compose.yml")).toBeInTheDocument();
    expect(screen.getByText(":2-9")).toBeInTheDocument();
  });

  // An explicit upload is materialized to disk by the native executor, which
  // injects an *absolute* "[Attached: <bridge>/uploads/…]" marker for the CLI.
  // The upload already rides in as an input_image / input_file block, so the
  // marker must NOT also surface as a path chip (it would double-render, and
  // the path is an internal temp dir).
  it("does not chip an absolute upload marker (already shown via its file block)", () => {
    renderBubble(
      userBubble(
        "[Attached: /var/folders/x/omnigent-1/claude-native/abc/uploads/image.png]\n\nwhat is this",
      ),
    );
    // No "@…" chip for the absolute upload path.
    expect(screen.queryByText(/^@\//)).toBeNull();
    expect(screen.queryByText(/uploads\/image\.png/)).toBeNull();
    // The marker is still stripped from the body and the prose survives.
    expect(screen.queryByText(/\[Attached:/)).toBeNull();
    expect(screen.getByText("what is this")).toBeInTheDocument();
  });

  // The absolute-path heuristic is OS-agnostic so it still suppresses the
  // chip if an executor ever materializes an upload on a Windows host
  // (drive-letter or UNC root), where the marker wouldn't start with "/".
  it.each([
    ["C:\\Users\\me\\AppData\\Local\\Temp\\omnigent\\uploads\\image.png", "drive (backslash)"],
    ["C:/Users/me/AppData/Local/Temp/omnigent/uploads/image.png", "drive (forward slash)"],
    ["\\\\host\\share\\omnigent\\uploads\\image.png", "UNC"],
  ])("does not chip a Windows-style absolute upload marker (%s)", (path) => {
    renderBubble(userBubble(`[Attached: ${path}]\n\nwhat is this`));
    expect(screen.queryByText(/uploads/)).toBeNull();
    expect(screen.getByText("what is this")).toBeInTheDocument();
  });

  it("chips a relative @-mention but not an absolute upload in the same message", () => {
    renderBubble(
      userBubble(
        "[Attached: /tmp/omnigent/claude-native/abc/uploads/image.png]\n" +
          "[Attached: src/server.ts]\n\ncompare",
      ),
    );
    // Workspace @-mention still chips...
    expect(screen.getByText("@src/server.ts")).toBeInTheDocument();
    // ...the materialized upload does not.
    expect(screen.queryByText(/uploads\/image\.png/)).toBeNull();
  });
});
