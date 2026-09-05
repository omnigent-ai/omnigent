import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ExtensionContext,
  ExtensionLifecycle,
} from "@omnigent/extension-sdk";

const harness = vi.hoisted(() => ({
  lifecycle: null as ExtensionLifecycle | null,
  render: vi.fn(),
  unmount: vi.fn(),
}));

vi.mock("@omnigent/extension-sdk", () => ({
  defineExtension: (lifecycle: ExtensionLifecycle) => {
    harness.lifecycle = lifecycle;
  },
}));
vi.mock("react-dom/client", () => ({
  createRoot: () => ({ render: harness.render, unmount: harness.unmount }),
}));
vi.mock("./CanvasApp", () => ({ CanvasApp: () => null }));

import "./main";

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  harness.render.mockClear();
  harness.unmount.mockClear();
});

describe("Canvas extension activation", () => {
  it("does not report ready before Canvas has meaningful content", async () => {
    const dispose = vi.fn();
    const context = {
      theme: {
        subscribe: vi.fn(async () => ({ dispose })),
      },
    } as unknown as ExtensionContext;

    const lifecycle = harness.lifecycle;
    if (!lifecycle) throw new Error("Canvas lifecycle was not registered");
    const activation = lifecycle.activate(context);
    await Promise.resolve();

    const app = harness.render.mock.calls[0]?.[0] as
      ReactElement<{ onReady: () => void }> | undefined;
    if (!app) throw new Error("Canvas was not rendered");
    let activated = false;
    void Promise.resolve(activation).then(() => {
      activated = true;
    });
    await Promise.resolve();
    expect(activated).toBe(false);

    app.props.onReady();
    await activation;

    expect(activated).toBe(true);
    await lifecycle.deactivate?.();
    expect(dispose).toHaveBeenCalledOnce();
    expect(harness.unmount).toHaveBeenCalledOnce();
  });
});
