import type { RenderResult } from "@testing-library/react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import { MermaidPreview } from "@/shell/MermaidPreview";
import { MermaidBlock, MermaidFullscreenProvider } from "./MermaidBlock";
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
  const diagramSelector = 'svg[id^="mermaid-"]';
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

  async function inlineDiagram() {
    const selector = `.chat-mermaid ${diagramSelector}`;
    await waitFor(() => expect(root.querySelector(selector)).not.toBeNull(), { timeout: 10_000 });
    return root.querySelector(selector)!;
  }

  const settlingReply = (mode: "streaming" | "static") => (
    <MermaidFullscreenProvider>
      <MessageResponse key={mode} mode={mode}>
        {markdown}
      </MessageResponse>
    </MermaidFullscreenProvider>
  );

  const page = (path: string) => (
    <MermaidFullscreenProvider resetKey={path}>
      <MessageResponse>{markdown}</MessageResponse>
    </MermaidFullscreenProvider>
  );

  function overlayDiagram() {
    const overlay = screen.getByTitle("Exit fullscreen").parentElement!;
    expect(overlay.parentElement).toBe(embedded ? root : document.body);
    return overlay.querySelector(diagramSelector);
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
      render(content(), { container: root });
      const svg = await inlineDiagram();
      expect(svg.textContent).toContain("Alpha");
      expect(screen.getAllByTitle("View fullscreen")).toHaveLength(1);
      fireEvent.click(screen.getByTitle("View fullscreen"));
      expect(overlayDiagram()?.outerHTML).toBe(svg.outerHTML);
      fireEvent.click(screen.getByTitle("Exit fullscreen"));
      expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
      fireEvent.click(screen.getByTitle("View fullscreen"));
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
    },
    15_000,
  );

  it("opens a diagram clicked before it finishes drawing", async () => {
    render(<MessageResponse>{markdown}</MessageResponse>, { container: root });
    fireEvent.click(screen.getByTitle("View fullscreen"));
    expect(root.querySelector(diagramSelector)).toBeNull();

    await screen.findByTitle("Exit fullscreen", {}, { timeout: 10_000 });
    expect(overlayDiagram()?.outerHTML).toBe((await inlineDiagram()).outerHTML);
  }, 15_000);

  it("opens a diagram clicked before its settled reply finishes drawing", async () => {
    const { rerender } = render(settlingReply("streaming"), { container: root });
    const clickedBlock = root.querySelector(".chat-mermaid");
    fireEvent.click(screen.getByTitle("View fullscreen"));
    rerender(settlingReply("static"));
    expect(clickedBlock?.isConnected).toBe(false);
    expect(root.querySelector(diagramSelector)).toBeNull();

    await screen.findByTitle("Exit fullscreen", {}, { timeout: 10_000 });
    expect(overlayDiagram()?.outerHTML).toBe((await inlineDiagram()).outerHTML);
  }, 15_000);

  it.each([
    [
      "while its fence was still streaming",
      ["flowchart LR\n  Alpha", "flowchart LR\n  Alpha --> G"],
      false,
    ],
    ["when an earlier diagram starts with the streamed part", ["flowchart LR\n  Alpha"], false],
    [
      "before a diagram above it unmounted",
      ["flowchart LR\n  Alpha", "flowchart LR\n  Alpha --> G"],
      true,
    ],
  ] as const)(
    "opens a diagram clicked %s",
    async (_case, [clickedFirst, ...clickedLater], startsWithRowAbove) => {
      const clicked = "flowchart LR\n  Alpha --> Gamma";
      const reply = (mode: "streaming" | "static", clickedSoFar: string, withRowAbove = false) => (
        <MermaidFullscreenProvider>
          {withRowAbove && <MessageResponse>{markdown}</MessageResponse>}
          <MessageResponse key={mode} mode={mode}>
            {`${markdown}\n\n\`\`\`mermaid\n${clickedSoFar}${mode === "static" ? "\n```" : ""}`}
          </MessageResponse>
        </MermaidFullscreenProvider>
      );
      const { rerender } = render(reply("streaming", clickedFirst, startsWithRowAbove), {
        container: root,
      });
      const clickedBlock = Array.from(root.querySelectorAll(".chat-mermaid")).at(-1)!;
      fireEvent.click(screen.getAllByTitle("View fullscreen").at(-1)!);
      for (const clickedSoFar of clickedLater) {
        rerender(reply("streaming", clickedSoFar));
        expect(clickedBlock.isConnected).toBe(true);
      }
      rerender(reply("static", clicked));
      expect(clickedBlock.isConnected).toBe(false);
      expect(root.querySelector(diagramSelector)).toBeNull();

      await screen.findByTitle("Exit fullscreen", {}, { timeout: 10_000 });
      const drawn = () => root.querySelectorAll(`.chat-mermaid ${diagramSelector}`);
      await waitFor(() => expect(drawn()).toHaveLength(2), { timeout: 10_000 });
      expect(overlayDiagram()?.textContent).toContain("Gamma");
      expect(overlayDiagram()?.outerHTML).toBe(drawn()[1].outerHTML);
    },
    15_000,
  );

  it.each([
    [4_999, true],
    [5_000, false],
  ])("opens a diagram drawn %i ms after its click: %s", async (elapsedMs, opens) => {
    vi.useFakeTimers();
    try {
      render(<MermaidBlock block={<pre>{source}</pre>} />, { container: root });
      fireEvent.click(screen.getByTitle("View fullscreen"));
      act(() => vi.advanceTimersByTime(elapsedMs));
      const lateDiagram = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      lateDiagram.id = "mermaid-late";
      await act(async () => root.querySelector(".chat-mermaid")!.append(lateDiagram));
      expect(screen.queryByTitle("Exit fullscreen") !== null).toBe(opens);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["replace the open diagram", ["drawing", "drawn"], false],
    ["reopen after dismissal", ["drawn", "drawing"], true],
  ] as const)(
    "does not let a pending click %s",
    async (_case, clicks, dismiss) => {
      const replies = (withDrawing: boolean) => (
        <MermaidFullscreenProvider>
          <MessageResponse>{"```mermaid\nflowchart LR\n  Gamma --> Delta\n```"}</MessageResponse>
          {withDrawing && <MessageResponse>{markdown}</MessageResponse>}
        </MermaidFullscreenProvider>
      );
      const { rerender } = render(replies(false), { container: root });
      await inlineDiagram();
      rerender(replies(true));
      const [drawn, drawing] = screen.getAllByTitle("View fullscreen");
      expect(root.querySelectorAll(`.chat-mermaid ${diagramSelector}`)).toHaveLength(1);
      for (const button of clicks) fireEvent.click({ drawn, drawing }[button]);
      expect(overlayDiagram()?.textContent).toContain("Gamma");
      if (dismiss) fireEvent.keyDown(window, { key: "Escape" });

      const bothDrawn = () =>
        expect(root.querySelectorAll(`.chat-mermaid ${diagramSelector}`)).toHaveLength(2);
      await waitFor(bothDrawn, { timeout: 10_000 });
      if (dismiss) expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
      else expect(overlayDiagram()?.textContent).toContain("Gamma");
    },
    15_000,
  );

  it("keeps the diagram open when a settled reply remounts its block", async () => {
    const { rerender } = render(settlingReply("streaming"), { container: root });
    const streamed = await inlineDiagram();
    fireEvent.click(screen.getByTitle("View fullscreen"));
    const openDiagram = overlayDiagram()?.outerHTML;
    expect(openDiagram).toBe(streamed.outerHTML);

    rerender(settlingReply("static"));
    expect(streamed.isConnected).toBe(false);
    expect(overlayDiagram()?.outerHTML).toBe(openDiagram);
    await inlineDiagram();
    expect(overlayDiagram()?.outerHTML).toBe(openDiagram);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  }, 15_000);

  it("closes the diagram when the route changes", async () => {
    const { rerender } = render(page("/c/first"), { container: root });
    await inlineDiagram();
    fireEvent.click(screen.getByTitle("View fullscreen"));
    expect(overlayDiagram()).not.toBeNull();

    rerender(page("/c/first"));
    expect(overlayDiagram()).not.toBeNull();
    rerender(page("/c/second"));
    expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
  }, 15_000);

  it.each([
    ["a route change", (rerender: RenderResult["rerender"]) => rerender(page("/c/second"))],
    ["Escape", () => fireEvent.keyDown(window, { key: "Escape" })],
  ])(
    "does not open a diagram clicked before %s once it draws",
    async (_cancellation, cancel) => {
      const { rerender } = render(page("/c/first"), { container: root });
      fireEvent.click(screen.getByTitle("View fullscreen"));
      cancel(rerender);
      await inlineDiagram();
      await act(async () => {});
      expect(screen.queryByTitle("Exit fullscreen")).toBeNull();
    },
    15_000,
  );
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
