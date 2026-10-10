import { beforeEach, describe, expect, it, vi } from "vitest";
import { mermaid, serializeSvgAsXml } from "./mermaidPlugin";
import { STREAMDOWN_PLUGINS } from "./streamdown-security";

const upstream = vi.hoisted(() => ({
  getMermaid: vi.fn(),
  initialize: vi.fn(),
  render: vi.fn(),
}));

vi.mock("@streamdown/mermaid", () => ({
  mermaid: {
    name: "mermaid",
    type: "diagram",
    language: "mermaid",
    getMermaid: upstream.getMermaid,
  },
}));

// The shape mermaid 11 returns under securityLevel "strict": DOMPurify's HTML
// serialisation of a flowchart whose node label spans several lines.
const HTML_SERIALISED_SVG =
  '<svg aria-roledescription="flowchart-v2" role="graphics-document document" ' +
  'viewBox="0 0 300 98" style="max-width: 300px;" class="flowchart" ' +
  'xmlns="http://www.w3.org/2000/svg" width="100%" id="mermaid-1">' +
  "<style>#mermaid-1{font-family:monospace;}#mermaid-1 .node&gt;rect{fill:#ECECFF;}</style>" +
  '<g><g class="node default" id="flowchart-A-0" transform="translate(70, 49)">' +
  '<rect class="basic label-container" x="-62" y="-41" width="124" height="82"></rect>' +
  '<g class="label" transform="translate(-32, -26)"><foreignObject width="64" height="52">' +
  '<div xmlns="http://www.w3.org/1999/xhtml" style="display: table-cell; white-space: nowrap;">' +
  '<span class="nodeLabel"><p>Databricks machines<br>every&nbsp;process;<br>event</p></span>' +
  "</div></foreignObject></g></g></g></svg>";

function parseXml(svg: string): Document {
  return new DOMParser().parseFromString(svg, "image/svg+xml");
}

function xmlParseError(svg: string): string | null {
  return parseXml(svg).querySelector("parsererror")?.textContent ?? null;
}

beforeEach(() => {
  upstream.getMermaid.mockReset();
  upstream.initialize.mockReset();
  upstream.render.mockReset();
  upstream.getMermaid.mockReturnValue({
    initialize: upstream.initialize,
    render: upstream.render,
  });
  upstream.render.mockResolvedValue({ svg: HTML_SERIALISED_SVG });
});

describe("serializeSvgAsXml", () => {
  it("turns mermaid's HTML-serialised labels into a well-formed SVG document", () => {
    expect(xmlParseError(HTML_SERIALISED_SVG)).not.toBeNull();

    const xml = serializeSvgAsXml(HTML_SERIALISED_SVG);

    expect(xmlParseError(xml)).toBeNull();
    expect(xml).not.toContain("&nbsp;");
    const doc = parseXml(xml);
    const root = doc.documentElement;
    expect(root.namespaceURI).toBe("http://www.w3.org/2000/svg");
    expect(root.getAttribute("aria-roledescription")).toBe("flowchart-v2");
    expect(root.getAttribute("width")).toBe("100%");
    const label = doc.querySelector("foreignObject p");
    expect(label?.namespaceURI).toBe("http://www.w3.org/1999/xhtml");
    expect(label?.querySelectorAll("br")).toHaveLength(2);
    expect(label?.textContent).toBe("Databricks machinesevery process;event");
    expect(doc.querySelector("style")?.textContent).toContain("#mermaid-1 .node>rect");
  });

  it("is stable once the markup is XML", () => {
    const xml = serializeSvgAsXml(HTML_SERIALISED_SVG);
    expect(serializeSvgAsXml(xml)).toBe(xml);
  });

  it("returns markup without an svg root unchanged", () => {
    expect(serializeSvgAsXml("<p>not a diagram</p>")).toBe("<p>not a diagram</p>");
    expect(serializeSvgAsXml("")).toBe("");
  });
});

describe("mermaid plugin", () => {
  it("keeps the identity Streamdown matches mermaid fences by", () => {
    expect(mermaid).toMatchObject({ name: "mermaid", type: "diagram", language: "mermaid" });
  });

  it("forwards the config and initialize calls to Streamdown's plugin", () => {
    const instance = mermaid.getMermaid({ theme: "dark" });
    expect(upstream.getMermaid).toHaveBeenCalledWith({ theme: "dark" });

    instance.initialize({ theme: "default" });
    expect(upstream.initialize).toHaveBeenCalledWith({ theme: "default" });
  });

  it("renders through Streamdown's plugin and hands back the SVG as XML", async () => {
    const { svg } = await mermaid.getMermaid().render("mermaid-1", "flowchart LR\n  A --> B");

    expect(upstream.render).toHaveBeenCalledWith("mermaid-1", "flowchart LR\n  A --> B");
    expect(svg).toBe(serializeSvgAsXml(HTML_SERIALISED_SVG));
    expect(xmlParseError(svg)).toBeNull();
  });

  it("is the plugin chat markdown renders diagrams with", () => {
    expect(STREAMDOWN_PLUGINS.mermaid).toBe(mermaid);
  });
});
