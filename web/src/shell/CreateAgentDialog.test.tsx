import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { PendingAgentSource } from "@/lib/agentBundle";

// jsdom lacks CompressionStream (used by the bundle builder for the File /
// Folder tabs). Install a passthrough so the tar bytes flow uncompressed.
class PassthroughStream {
  readable: ReadableStream;
  writable: WritableStream;
  constructor() {
    let controller: ReadableStreamDefaultController;
    this.readable = new ReadableStream({
      start(c) {
        controller = c;
      },
    });
    this.writable = new WritableStream({
      write(chunk) {
        controller.enqueue(new Uint8Array(chunk));
      },
      close() {
        controller.close();
      },
    });
  }
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).CompressionStream = PassthroughStream;

const { CreateAgentDialog } = await import("./CreateAgentDialog");

afterEach(cleanup);

function renderDialog() {
  const onCreate = vi.fn<(source: PendingAgentSource) => void>();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <CreateAgentDialog open onOpenChange={() => {}} onCreate={onCreate} />
    </QueryClientProvider>,
  );
  return onCreate;
}

/** Radix Tabs activates a trigger on mousedown (not click), so fire both. */
function switchTab(testId: string) {
  const trigger = screen.getByTestId(testId);
  fireEvent.mouseDown(trigger);
  fireEvent.click(trigger);
}

/** Build a File with a webkitRelativePath (folder-pick shape). */
function fileWithPath(path: string, content: string): File {
  const file = new File([content], path.split("/").pop() ?? path, { type: "text/yaml" });
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

describe("CreateAgentDialog tabs", () => {
  it("Form tab emits a { kind: 'form' } source", () => {
    const onCreate = renderDialog();
    fireEvent.change(screen.getByTestId("create-agent-name"), {
      target: { value: "my-agent" },
    });
    fireEvent.change(screen.getByTestId("create-agent-model"), {
      target: { value: "claude-sonnet-4-20250514" },
    });
    fireEvent.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledTimes(1);
    const source = onCreate.mock.calls[0][0];
    expect(source.kind).toBe("form");
    if (source.kind === "form") {
      expect(source.input.name).toBe("my-agent");
      expect(source.input.model).toBe("claude-sonnet-4-20250514");
    }
  });

  it("GitHub tab emits a { kind: 'github' } source with ref + subdir", () => {
    const onCreate = renderDialog();
    switchTab("create-agent-tab-github");
    fireEvent.change(screen.getByTestId("create-agent-github-url"), {
      target: { value: "https://github.com/acme/bot" },
    });
    fireEvent.change(screen.getByTestId("create-agent-github-ref"), {
      target: { value: "v1.0.0" },
    });
    fireEvent.change(screen.getByTestId("create-agent-github-subdir"), {
      target: { value: "agents/foo" },
    });
    fireEvent.click(screen.getByTestId("create-agent-github-submit"));
    expect(onCreate).toHaveBeenCalledTimes(1);
    const source = onCreate.mock.calls[0][0];
    expect(source).toMatchObject({
      kind: "github",
      sourceUrl: "https://github.com/acme/bot",
      ref: "v1.0.0",
      subdir: "agents/foo",
    });
  });

  it("Folder tab builds a bundle File from picked files", async () => {
    const onCreate = renderDialog();
    switchTab("create-agent-tab-folder");
    const input = screen.getByTestId("create-agent-folder-input") as HTMLInputElement;
    const files = [
      fileWithPath("my-agent/config.yaml", "spec_version: 1\nname: x"),
      fileWithPath("my-agent/AGENTS.md", "hello"),
    ];
    fireEvent.change(input, { target: { files } });
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    const source = onCreate.mock.calls[0][0];
    expect(source.kind).toBe("bundle");
    if (source.kind === "bundle") {
      expect(source.bundle).toBeInstanceOf(File);
      expect(source.bundle.name).toBe("agent.tar.gz");
    }
  });

  it("Folder tab surfaces a validation error and does not emit", async () => {
    const onCreate = renderDialog();
    switchTab("create-agent-tab-folder");
    const input = screen.getByTestId("create-agent-folder-input") as HTMLInputElement;
    // No top-level YAML → buildBundleFromFiles throws.
    fireEvent.change(input, {
      target: { files: [fileWithPath("my-agent/AGENTS.md", "hi")] },
    });
    await waitFor(() =>
      expect(screen.getByTestId("create-agent-upload-error")).toBeInTheDocument(),
    );
    expect(onCreate).not.toHaveBeenCalled();
  });
});
