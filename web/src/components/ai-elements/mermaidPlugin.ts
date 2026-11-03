import { mermaid as upstreamMermaid, type DiagramPlugin } from "@streamdown/mermaid";

export const mermaid: DiagramPlugin = {
  ...upstreamMermaid,
  getMermaid(config) {
    const instance = upstreamMermaid.getMermaid(config);
    return {
      ...instance,
      async render(id, source) {
        const result = await instance.render(id, source);
        // Mermaid's sanitizer emits HTML void tags such as <br> in SVG labels.
        // Serialize the sanitized DOM as XML so PNG's image decoder accepts it.
        const container = document.createElement("div");
        container.innerHTML = result.svg;
        return {
          ...result,
          svg: new XMLSerializer().serializeToString(container.firstElementChild!),
        };
      },
    };
  },
};
