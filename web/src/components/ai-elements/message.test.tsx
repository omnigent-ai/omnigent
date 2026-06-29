import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageResponse } from "./message";

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

    fireEvent.click(await screen.findByRole("button", { name: "Copy Code" }));

    await waitFor(() => {
      expect(copiedText).toEqual(["const value = 1;\nconsole.log(value);\n"]);
    });
    expect(screen.getByRole("button", { name: "Download file" })).toBeInTheDocument();
  });
});
