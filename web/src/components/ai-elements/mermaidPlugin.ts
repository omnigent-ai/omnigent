// Mermaid returns its SVG HTML-serialised: under securityLevel "strict",
// DOMPurify re-emits multi-line <foreignObject> labels with bare <br> and
// &nbsp;, which no XML parser accepts, yet Streamdown saves that string as
// diagram.svg and rasterises it through <img> for PNG.
import {
  mermaid as streamdownMermaid,
  type DiagramPlugin,
  type MermaidInstance,
} from "@streamdown/mermaid";

const XHTML_NS = "http://www.w3.org/1999/xhtml";

/** Re-serialise an HTML-serialised SVG as well-formed XML; other input is returned unchanged. */
export function serializeSvgAsXml(svg: string): string {
  if (typeof DOMParser === "undefined" || typeof XMLSerializer === "undefined") return svg;
  const root = new DOMParser().parseFromString(svg, "text/html").querySelector("svg");
  if (!root) return svg;
  // The HTML parser keeps the labels' xmlns as a plain attribute, which a
  // spec-literal XMLSerializer then emits beside its own declaration.
  for (const element of Array.from(root.getElementsByTagNameNS(XHTML_NS, "*"))) {
    element.removeAttribute("xmlns");
  }
  return new XMLSerializer().serializeToString(root);
}

function withXmlSvg(instance: MermaidInstance): MermaidInstance {
  return {
    initialize: (config) => instance.initialize(config),
    render: async (id, source) => {
      const result = await instance.render(id, source);
      return { ...result, svg: serializeSvgAsXml(result.svg) };
    },
  };
}

/**
 * Streamdown's mermaid plugin, with every rendered SVG usable as a standalone
 * file. Streamdown's download menu renders through the plugin, so this is the
 * only seam the app controls on that path.
 */
export const mermaid: DiagramPlugin = {
  ...streamdownMermaid,
  getMermaid: (config) => withXmlSvg(streamdownMermaid.getMermaid(config)),
};
