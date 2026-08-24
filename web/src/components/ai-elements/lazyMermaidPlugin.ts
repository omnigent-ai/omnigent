import type { DiagramPlugin, MermaidConfig, MermaidInstance } from "@streamdown/mermaid";

// Streamdown's `mermaid` plugin (@streamdown/mermaid) statically imports
// mermaid, which drags cytoscape and the hand-drawn renderer into the eager
// entry graph even when no message ever contains a diagram.
//
// This wrapper defers the @streamdown/mermaid import until a diagram actually
// renders, mirroring `lazyCodePlugin`, so mermaid splits into its own on-demand
// chunk. Streamdown only ever reaches the instance as
// `getMermaid(config).render(...)` from an async path, so a promise-returning
// `render` satisfies the contract; `language` is the one field it needs
// synchronously, to match the ```mermaid fence.

let realMermaid: DiagramPlugin | null = null;
let mermaidPromise: Promise<DiagramPlugin> | null = null;

const loadMermaid = (): Promise<DiagramPlugin> => {
  // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then)
  mermaidPromise ??= import("@streamdown/mermaid").then(({ mermaid }) => {
    realMermaid = mermaid;
    return mermaid;
  });
  return mermaidPromise;
};

export const lazyMermaidPlugin: DiagramPlugin = {
  name: "mermaid",
  type: "diagram",
  language: "mermaid",
  getMermaid: (config?: MermaidConfig): MermaidInstance => {
    let pending: MermaidConfig | undefined;
    const engine = async () => {
      const plugin = realMermaid ?? (await loadMermaid());
      const instance = plugin.getMermaid(config);
      if (pending) {
        instance.initialize(pending);
        pending = undefined;
      }
      return instance;
    };
    return {
      // Streamdown passes its config to getMermaid and never calls initialize,
      // but the contract allows it. Hold the config rather than racing a
      // floating import, so initialize-then-render keeps that order.
      initialize: (nextConfig: MermaidConfig) => {
        pending = nextConfig;
      },
      render: async (id: string, source: string) => (await engine()).render(id, source),
    };
  },
};
