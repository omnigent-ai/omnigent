import { expect, it, vi } from "vitest";
import { mermaid } from "./mermaidPlugin";

const upstream = vi.hoisted(() => ({
  getMermaid: vi.fn(),
  render: vi.fn(),
  initialize: vi.fn(),
}));

vi.mock("@streamdown/mermaid", () => ({
  mermaid: { getMermaid: upstream.getMermaid },
}));

it("keeps sanitized HTML labels valid XML while preserving the Mermaid render contract", async () => {
  const bindFunctions = vi.fn();
  upstream.getMermaid.mockReturnValue({
    render: upstream.render,
    initialize: upstream.initialize,
  });
  upstream.render.mockResolvedValue({
    svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><foreignObject><div><p>Client<br>Second line &amp; 日本語</p></div></foreignObject></svg>',
    bindFunctions,
  });

  const config = { theme: "dark" } as const;
  const instance = mermaid.getMermaid(config);
  expect(upstream.getMermaid).toHaveBeenCalledWith(config);
  expect(instance.initialize).toBe(upstream.initialize);

  const source = 'flowchart LR\n  A["Client<br/>Second line & 日本語"]';
  const result = await instance.render("diagram", source);
  expect(upstream.render).toHaveBeenCalledWith("diagram", source);
  expect(result).toMatchObject({ bindFunctions });

  const svg = new DOMParser().parseFromString(result.svg, "image/svg+xml");
  expect(svg.querySelector("parsererror")).toBeNull();
  expect(svg.documentElement.getAttribute("viewBox")).toBe("0 0 100 50");
  expect(svg.querySelector("br")?.namespaceURI).toBe("http://www.w3.org/1999/xhtml");
  expect(svg.querySelector("p")?.textContent).toBe("ClientSecond line & 日本語");
});
