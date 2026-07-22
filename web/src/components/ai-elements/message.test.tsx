import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Message, MessageContent, MessageResponse } from "./message";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

afterEach(() => {
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
  it("uses dedicated theme-specific user bubble colors", () => {
    render(
      <Message from="user">
        <MessageContent>Theme-aware prompt</MessageContent>
      </Message>,
    );

    const prompt = screen.getByText("Theme-aware prompt");
    expect(prompt).toHaveClass(
      "group-[.is-user]:rounded-[var(--radius-otto-md)]",
      "group-[.is-user]:bg-user-bubble",
      "group-[.is-user]:text-user-bubble-foreground",
      "group-[.is-user]:ring-user-bubble-border",
    );
    expect(prompt).not.toHaveClass("group-[.is-user]:bg-muted", "group-[.is-user]:text-foreground");
  });

  it("uses readable theme-specific assistant typography", () => {
    render(
      <Message from="assistant">
        <MessageContent>Theme-aware response</MessageContent>
      </Message>,
    );

    const response = screen.getByText("Theme-aware response");
    expect(response).toHaveClass(
      "group-[.is-assistant]:text-[14px]",
      "group-[.is-assistant]:leading-5",
      "group-[.is-assistant]:text-assistant-foreground",
    );
  });
});

describe("MessageResponse", () => {
  it("blocks external image markdown and renders a placeholder", async () => {
    render(<MessageResponse>{"![leak](https://attacker.example/pixel.png)"}</MessageResponse>);

    expect(document.querySelector('img[src^="https://attacker.example"]')).toBeNull();
    expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
  });
});

describe("MessageResponse code-block copy", () => {
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

    const wrapButton = await screen.findByRole("button", { name: "Toggle word wrap" });
    const copyButton = screen.getByRole("button", { name: "Copy Code" });
    expect(wrapButton).toHaveClass("size-6");
    expect(copyButton).toHaveClass("size-6");
    expect(wrapButton).toHaveClass("rounded-[var(--radius-otto-xs)]");
    expect(copyButton).toHaveClass("rounded-[var(--radius-otto-xs)]");

    fireEvent.click(copyButton);

    await waitFor(() => {
      expect(copiedText).toEqual(["const value = 1;\nconsole.log(value);\n"]);
    });
    expect(screen.getByRole("button", { name: "Download file" })).toBeInTheDocument();
  });
});
