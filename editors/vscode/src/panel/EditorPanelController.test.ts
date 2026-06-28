/**
 * Tests for EditorPanelController — the sole owner of the editor WebviewPanel.
 * Uses a FAKE WebviewPanel (createWebviewPanel replaced) and a LOCAL target so
 * the iframe render path is taken (the only path in this build).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as vscode from "vscode";
import { EditorPanelController } from "./EditorPanelController";
import type { ServerTarget } from "../config";

const LOCAL_TARGET: ServerTarget = {
  baseUrl: "http://127.0.0.1:6767",
  origin: "http://127.0.0.1:6767",
  hostType: "local",
  source: "discovered",
};

/** A fake WebviewPanel that records html set on its webview. */
function makeFakePanel() {
  let disposeCb: (() => void) | undefined;
  const panel = {
    webview: {
      html: "",
      cspSource: "vscode-resource:",
      asWebviewUri: (uri: { toString(): string }) => uri,
      postMessage: () => true,
    },
    reveal: vi.fn(),
    onDidDispose: (cb: () => void) => {
      disposeCb = cb;
      return { dispose: () => {} };
    },
    dispose: vi.fn(() => disposeCb?.()),
  };
  return { panel };
}

function makeController() {
  const extensionUri = vscode.Uri.parse("file:///ext") as unknown as vscode.Uri;
  const output = { appendLine: () => {} } as unknown as vscode.OutputChannel;
  return new EditorPanelController(extensionUri, output);
}

describe("EditorPanelController", () => {
  let fake: ReturnType<typeof makeFakePanel>;
  let createSpy: ReturnType<typeof vi.fn<unknown[], unknown>>;

  beforeEach(() => {
    fake = makeFakePanel();
    const win = vscode.window as unknown as Record<string, unknown>;
    createSpy = vi.fn<unknown[], unknown>(() => fake.panel);
    win.createWebviewPanel = createSpy;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("ensure() creates a single panel and renders the iframe for a resolved local target", () => {
    const controller = makeController();
    controller.setResolved(LOCAL_TARGET);
    controller.ensure();

    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(fake.panel.webview.html).toContain('id="omnigent-frame"');
    expect(fake.panel.webview.html).toContain('src="http://127.0.0.1:6767"');
  });

  it("ensure() reuses and reveals the existing panel (no second panel)", () => {
    const controller = makeController();
    controller.setResolved(LOCAL_TARGET);
    controller.ensure();
    controller.ensure();

    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(fake.panel.reveal).toHaveBeenCalled();
  });

  it("opening before setResolved shows the placeholder, then setResolved re-renders the iframe", () => {
    const controller = makeController();

    controller.ensure();
    expect(controller.isOpen()).toBe(true);
    expect(fake.panel.webview.html).toContain("Resolving");

    controller.setResolved(LOCAL_TARGET);
    expect(fake.panel.webview.html).toContain('id="omnigent-frame"');
    expect(createSpy).toHaveBeenCalledTimes(1);
  });

  it("never injects a token into the rendered html", () => {
    const controller = makeController();
    controller.setResolved(LOCAL_TARGET);
    controller.ensure();
    expect(fake.panel.webview.html.toLowerCase()).not.toContain("token");
  });

  it("dispose() disposes the panel and nulls the ref; onDidDispose does not double-clear", () => {
    const controller = makeController();
    controller.setResolved(LOCAL_TARGET);
    controller.ensure();
    expect(controller.isOpen()).toBe(true);

    controller.dispose();
    expect(fake.panel.dispose).toHaveBeenCalled();
    expect(controller.isOpen()).toBe(false);
  });
});
