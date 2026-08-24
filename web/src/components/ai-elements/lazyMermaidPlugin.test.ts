import { beforeEach, describe, expect, it, vi } from "vitest";

// Stand in for @streamdown/mermaid so the test can observe *when* the real
// module is reached. Rendering a diagram for real needs SVG layout APIs
// (getBBox) that jsdom does not implement.
const calls: string[] = [];
const getMermaid = vi.fn((config?: unknown) => ({
  initialize: vi.fn(() => calls.push("initialize")),
  render: vi.fn(async (id: string) => {
    calls.push("render");
    return { svg: `<svg data-id="${id}" data-config="${!!config}" />` };
  }),
}));

vi.mock("@streamdown/mermaid", () => ({
  mermaid: { name: "mermaid", type: "diagram", language: "mermaid", getMermaid },
}));

// Guards the shape Streamdown depends on, and that mermaid stays off the
// module-load path. Streamdown reads `language` synchronously to match a
// ```mermaid fence, then calls `getMermaid(config).render(...)` from an async
// path -- so `render` may return a promise, which is what lets the import wait.
describe("lazyMermaidPlugin", () => {
  beforeEach(() => {
    vi.resetModules();
    getMermaid.mockClear();
    calls.length = 0;
  });

  it("exposes the diagram plugin contract synchronously", async () => {
    const { lazyMermaidPlugin } = await import("./lazyMermaidPlugin");

    expect(lazyMermaidPlugin.name).toBe("mermaid");
    expect(lazyMermaidPlugin.type).toBe("diagram");
    expect(lazyMermaidPlugin.language).toBe("mermaid");
    // Reading the contract must not reach the engine.
    expect(getMermaid).not.toHaveBeenCalled();
  });

  it("does not touch the engine until a diagram renders", async () => {
    const { lazyMermaidPlugin } = await import("./lazyMermaidPlugin");

    const instance = lazyMermaidPlugin.getMermaid();
    expect(getMermaid).not.toHaveBeenCalled();

    const { svg } = await instance.render("diagram-1", "graph TD;\n  A-->B;");
    expect(getMermaid).toHaveBeenCalledTimes(1);
    expect(svg).toContain('data-id="diagram-1"');
  });

  it("forwards the config given to getMermaid", async () => {
    const { lazyMermaidPlugin } = await import("./lazyMermaidPlugin");

    const config = { theme: "dark" } as const;
    await lazyMermaidPlugin.getMermaid(config).render("diagram-2", "graph TD;\n  C-->D;");

    expect(getMermaid).toHaveBeenCalledWith(config);
  });

  it("applies a config from initialize before rendering", async () => {
    const { lazyMermaidPlugin } = await import("./lazyMermaidPlugin");

    const instance = lazyMermaidPlugin.getMermaid();
    instance.initialize({ theme: "forest" });
    await instance.render("diagram-3", "graph TD;\n  E-->F;");

    // initialize is synchronous on our wrapper, so it must not race the import:
    // the engine has to see it before the first render.
    expect(calls).toEqual(["initialize", "render"]);
  });
});
