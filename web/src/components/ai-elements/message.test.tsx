import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import { MermaidPreview } from "@/shell/MermaidPreview";
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
      vi.stubGlobal("IntersectionObserver", VisibleIntersectionObserver);
      // jsdom has no SVG layout; keep the real Mermaid renderer and supply its measurements.
      const originalGetBBox = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
      Object.defineProperty(SVGElement.prototype, "getBBox", {
        configurable: true,
        value: () => ({ x: 0, y: 0, width: 100, height: 40 }),
      });
      const root = document.createElement("div");
      root.className = "omnigent-app";
      document.body.appendChild(root);
      setEmbedRoot(embedded ? root : null);
      try {
        const { container } = render(content(), { container: root });
        await waitFor(
          () => {
            expect(container.querySelector('svg[id^="mermaid-"]')).not.toBeNull();
          },
          { timeout: 10_000 },
        );
        const svg = container.querySelector('svg[id^="mermaid-"]')!;
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
      } finally {
        cleanup();
        root.remove();
        if (originalGetBBox)
          Object.defineProperty(SVGElement.prototype, "getBBox", originalGetBBox);
        else delete (SVGElement.prototype as { getBBox?: unknown }).getBBox;
      }
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
