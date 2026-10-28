import {
  mermaid as streamdownMermaid,
  type DiagramPlugin,
  type MermaidInstance,
} from "@streamdown/mermaid";

function parseSvgAsHtml(markup: string): SVGSVGElement | null {
  return new DOMParser().parseFromString(markup, "text/html").querySelector("svg");
}

// The HTML parser keeps xmlns attributes that XMLSerializer then emits again
// beside its own declarations; neither changes the tree the HTML parser builds.
function removeNamespaceDeclarations(root: Element): void {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    for (const { name } of Array.from(element.attributes)) {
      if (name === "xmlns" || name.startsWith("xmlns:")) element.removeAttribute(name);
    }
  }
}

// Re-emit mermaid's HTML-serialised SVG (bare `<br>`, `&nbsp;`) as XML for files and `<img>`.
// Streamdown also injects it inline, so the XML is kept only if the HTML parser
// rebuilds the sanitiser's exact tree from it.
export function serializeSvgAsXml(svg: string): string {
  if (typeof DOMParser === "undefined" || typeof XMLSerializer === "undefined") return svg;
  const root = parseSvgAsHtml(svg);
  if (!root) return svg;
  removeNamespaceDeclarations(root);
  const xml = new XMLSerializer().serializeToString(root);
  const reparsed = parseSvgAsHtml(xml);
  if (!reparsed) return svg;
  removeNamespaceDeclarations(reparsed);
  return reparsed.isEqualNode(root) ? xml : svg;
}

function withXmlSvg(instance: MermaidInstance): MermaidInstance {
  return {
    ...instance,
    render: async (id, source) => {
      const result = await instance.render(id, source);
      return { ...result, svg: serializeSvgAsXml(result.svg) };
    },
  };
}

// Streamdown's download menu renders through the plugin, the only seam the app controls there.
export const mermaid: DiagramPlugin = {
  ...streamdownMermaid,
  getMermaid: (config) => withXmlSvg(streamdownMermaid.getMermaid(config)),
};
