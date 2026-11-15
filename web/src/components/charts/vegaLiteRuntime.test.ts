import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ChartDrawCancelled,
  ChartLimitError,
  clearRemoteSceneValues,
  hadRemoteSceneValues,
  blockedLoader,
  chartThemeConfig,
  createChartTooltip,
  renderVegaLiteChart,
  withContainerWidth,
  type ChartColors,
} from "./vegaLiteRuntime";

const COLORS: ChartColors = {
  foreground: "#111111",
  mutedForeground: "#666666",
  border: "#dddddd",
  background: "#ffffff",
  accent: "#3b8ff5",
  fontFamily: "Inter",
};

const BAR_SPEC = {
  data: {
    values: [
      { category: "A", amount: 28 },
      { category: "B", amount: 55 },
      { category: "C", amount: 43 },
    ],
  },
  mark: { type: "bar", tooltip: true },
  encoding: {
    x: { field: "category", type: "nominal" },
    y: { field: "amount", type: "quantitative" },
  },
};

function mountContainer(): HTMLDivElement {
  const container = document.createElement("div");
  document.body.append(container);
  return container;
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("renderVegaLiteChart", () => {
  it("draws an inline-data spec as SVG marks", async () => {
    const container = mountContainer();

    const chart = await renderVegaLiteChart(container, BAR_SPEC, () => COLORS);

    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelectorAll("g.mark-rect path")).toHaveLength(3);
    chart.finalize();
  });

  it("evaluates expressions without generating code", async () => {
    const generateCode = vi.spyOn(globalThis, "Function");
    const container = mountContainer();
    const spec = {
      ...BAR_SPEC,
      transform: [
        { calculate: "datum.amount * 2", as: "doubled" },
        { filter: "datum.doubled > 60" },
      ],
      encoding: { ...BAR_SPEC.encoding, y: { field: "doubled", type: "quantitative" } },
    };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    // Only B (110) and C (86) pass the filter.
    expect(container.querySelectorAll("g.mark-rect path")).toHaveLength(2);
    expect(generateCode).not.toHaveBeenCalled();
    chart.finalize();
  });

  it("applies the theme config under a spec's own config", async () => {
    const container = mountContainer();
    const spec = { ...BAR_SPEC, config: { axis: { labelColor: "#ff0000" } } };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    const labels = [...container.querySelectorAll("g.role-axis-label text")];
    expect(labels.length).toBeGreaterThan(0);
    expect(labels.every((label) => label.getAttribute("fill") === "#ff0000")).toBe(true);
    const title = container.querySelector("g.role-axis-title text");
    expect(title?.getAttribute("fill")).toBe(COLORS.foreground);
    chart.finalize();
  });

  it("does not load images named by a mark", async () => {
    const loadImage = vi.spyOn(globalThis, "Image");
    const container = mountContainer();
    const spec = {
      data: { values: [{ x: 1 }] },
      mark: { type: "image", width: 20, height: 20 },
      encoding: {
        x: { field: "x", type: "quantitative" },
        url: { value: "https://example.com/pixel.png" },
      },
    };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    expect(loadImage).not.toHaveBeenCalled();
    expect(container.innerHTML).not.toContain("example.com");
    chart.finalize();
  });

  it("rejects a spec that doesn't compile", async () => {
    const container = mountContainer();

    await expect(
      renderVegaLiteChart(container, { mark: "not-a-mark", data: { values: [] } }, () => COLORS),
    ).rejects.toThrow();
  });
});

describe("renderVegaLiteChart confinement", () => {
  const counter = (on: object) => ({
    data: { values: [{}] },
    params: [{ name: "count", value: 0, on: [on] }],
    mark: { type: "text", text: { expr: "'count ' + count" } },
  });

  async function pause(milliseconds: number): Promise<void> {
    await new Promise((resolve) => {
      setTimeout(resolve, milliseconds);
    });
  }

  it("keeps a bound input inside the chart", async () => {
    const outside = document.createElement("div");
    outside.id = "outside";
    outside.textContent = "app content";
    document.body.append(outside);
    const container = mountContainer();
    const spec = {
      ...BAR_SPEC,
      params: [{ name: "size", value: 1, bind: { input: "range", element: "#outside" } }],
    };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    expect(outside.textContent).toBe("app content");
    expect(container.querySelector('input[type="range"]')).not.toBeNull();
    chart.finalize();
  });

  it.each([
    ["keys pressed anywhere on the page", { events: "window:keydown", update: "count + 1" }],
    ["timers", { events: "timer{5}", update: "count + 1" }],
  ])("doesn't listen to %s", async (_label, on) => {
    const container = mountContainer();
    const chart = await renderVegaLiteChart(container, counter(on), () => COLORS);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    await pause(30);

    expect(container.querySelector("g.mark-text text")?.textContent).toBe("count 0");
    chart.finalize();
  });

  it("never asks the network for data that names a URL", async () => {
    const network = vi.spyOn(globalThis, "fetch");
    const container = mountContainer();
    const spec = { data: { url: "https://example.com/rows.json" }, mark: "point" };

    await renderVegaLiteChart(container, spec, () => COLORS).then(
      (chart) => chart.finalize(),
      () => undefined,
    );

    expect(network).not.toHaveBeenCalled();
  });

  it.each([
    [
      "a cursor image",
      { ...BAR_SPEC, mark: { type: "bar", cursor: "url(https://e.test/c.png), auto" } },
      /CSS keyword/,
    ],
    [
      "a computed cursor",
      { ...BAR_SPEC, mark: { type: "bar", cursor: { expr: "'pointer'" } } },
      /CSS keyword/,
    ],
    [
      "a cursor parameter",
      { ...BAR_SPEC, params: [{ name: "cursor", value: "pointer" }] },
      /cursor/,
    ],
    [
      "an escaped url() color",
      { ...BAR_SPEC, mark: { type: "bar", color: "u\\72l(https://e.test/p.svg#g)" } },
      /url/,
    ],
    [
      "url() in a data color",
      {
        data: { values: [{ x: 1, paint: "url(https://e.test/p.svg#g)" }] },
        mark: "point",
        encoding: {
          x: { field: "x", type: "quantitative" },
          color: { field: "paint", scale: null },
        },
      },
      /remote file/,
    ],
    [
      "a url() color an expression wraps in an array",
      {
        data: { values: [{ x: 1 }] },
        transform: [{ calculate: "['ur' + 'l(https://e.test/p.svg#g)']", as: "paint" }],
        mark: "point",
        encoding: {
          x: { field: "x", type: "quantitative" },
          color: { field: "paint", scale: null },
        },
      },
      /remote file/,
    ],
    [
      "a blend mode an expression builds into CSS",
      {
        ...BAR_SPEC,
        mark: {
          type: "bar",
          blend: { expr: "'multiply; fill: ur' + 'l(https://e.test/p.svg#g)'" },
        },
      },
      /remote file/,
    ],
    [
      "a url() color an expression builds",
      {
        data: { values: [{ x: 1 }] },
        transform: [{ calculate: "'ur' + 'l(https://e.test/p.svg#g)'", as: "paint" }],
        mark: "point",
        encoding: {
          x: { field: "x", type: "quantitative" },
          color: { field: "paint", scale: null },
        },
      },
      /remote file/,
    ],
  ])("refuses %s", async (_label, spec, message) => {
    const container = mountContainer();

    await expect(renderVegaLiteChart(container, spec, () => COLORS)).rejects.toThrow(message);
    expect(container.querySelector("svg")).toBeNull();
  });

  // One past each limit, so a regression fails cleanly instead of exhausting memory.
  it.each([
    ["a parenthesised sequence call", "length((sequence)(0, 100001))"],
    ["an escaped sequence call", "length(sequ\\u0065nce(0, 100001))"],
    ["a long pad", "length(pad('', 100001))"],
    ["a pad with a long pad string", "length(pad('', 1000, pad('', 101)))"],
    ["a numeric format width", "format(1, 1001)"],
    ["a time sequence", "length(timeSequence('milliseconds', 0, 100001))"],
  ])("fails %s instead of allocating it", async (_label, calculate) => {
    const container = mountContainer();
    const spec = { ...BAR_SPEC, transform: [{ calculate, as: "n" }] };

    await expect(renderVegaLiteChart(container, spec, () => COLORS)).rejects.toBeInstanceOf(
      ChartLimitError,
    );
  });

  it("keeps capped functions working within their limits", async () => {
    const container = mountContainer();
    const spec = {
      data: { values: [{ amount: 28 }] },
      transform: [
        {
          calculate:
            "length(sequence(0, 10)) + '|' + length(sequence(0, 10, 0)) + '|' + pad('7', 3, '0') + '|' + length(utcSequence('day', 0, 864000000))",
          as: "summary",
        },
      ],
      mark: "text",
      encoding: { text: { field: "summary" } },
    };
    const formatted = {
      data: { values: [{ amount: 28 }] },
      mark: "text",
      encoding: { text: { field: "amount", type: "quantitative", format: ".1f" } },
    };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);
    expect(container.querySelector("g.mark-text text")?.textContent).toBe("10|0|700|10");
    chart.finalize();

    const formattedChart = await renderVegaLiteChart(container, formatted, () => COLORS);
    expect(container.querySelector("g.mark-text text")?.textContent).toBe("28.0");
    formattedChart.finalize();
  });

  it("draws rows with ordinary cursor and url columns", async () => {
    const container = mountContainer();
    const spec = {
      data: {
        values: [
          { cursor: "page2", link: "see url(docs)", n: 1 },
          { cursor: 2, link: "", n: 2 },
        ],
      },
      mark: { type: "point", tooltip: true },
      encoding: { x: { field: "n", type: "quantitative" } },
    };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    expect(container.querySelectorAll("g.mark-symbol path")).toHaveLength(2);
    chart.finalize();
  });

  it("keeps the chart's own gradient fills", async () => {
    const container = mountContainer();
    const gradient = {
      gradient: "linear",
      stops: [
        { offset: 0, color: "white" },
        { offset: 1, color: "darkgreen" },
      ],
    };
    const spec = { ...BAR_SPEC, mark: { type: "bar", color: gradient } };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    // Vega points gradient fills at the page's own `#gradient_N` definitions.
    const fill = container.querySelector("g.mark-rect path")?.getAttribute("fill");
    expect(fill).toMatch(/^url\(.*#gradient_\d+\)$/);
    expect(await (await chart.toBlob("svg")).text()).toMatch(/fill="url\(.*#gradient_\d+\)"/);
    chart.finalize();
  });

  it("clears a url() color that appears after the first draw, on the page and in exports", async () => {
    const container = mountContainer();
    const spec = {
      data: { values: [{ x: 1 }, { x: 2 }] },
      params: [{ name: "p", value: 0, bind: { input: "range", min: 0, max: 1, step: 1 } }],
      mark: {
        type: "point",
        filled: true,
        fill: { expr: "p ? 'ur' + 'l(https://e.test/p.svg#g)' : 'red'" },
      },
      encoding: { x: { field: "x", type: "quantitative" } },
    };
    const writes = vi.spyOn(Element.prototype, "setAttribute");
    const chart = await renderVegaLiteChart(container, spec, () => COLORS);
    const slider = container.querySelector<HTMLInputElement>('input[type="range"]')!;

    slider.value = "1";
    slider.dispatchEvent(new Event("input"));
    await pause(50);

    expect(writes.mock.calls.some(([, value]) => String(value).includes("e.test"))).toBe(false);
    expect(container.innerHTML).not.toContain("e.test");
    expect(await (await chart.toBlob("svg")).text()).not.toContain("e.test");
    chart.finalize();
  });

  it("drops a spec background that would load a file, on the page and in exports", async () => {
    const container = mountContainer();
    const spec = { ...BAR_SPEC, background: { expr: "'ur' + 'l(https://e.test/p.svg#g)'" } };
    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    const svg = await (await chart.toBlob("svg")).text();

    expect(svg).not.toContain("e.test");
    expect(container.innerHTML).not.toContain("e.test");
    chart.finalize();
  });

  it("allows a keyword cursor", async () => {
    const container = mountContainer();
    const spec = { ...BAR_SPEC, mark: { type: "bar", cursor: "pointer" } };

    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    expect(container.querySelectorAll("g.mark-rect path")).toHaveLength(3);
    chart.finalize();
  });

  it("leaves the container alone when cancelled before drawing", async () => {
    const container = mountContainer();
    container.textContent = "newer chart";

    await expect(
      renderVegaLiteChart(
        container,
        BAR_SPEC,
        () => COLORS,
        () => true,
      ),
    ).rejects.toBeInstanceOf(ChartDrawCancelled);
    expect(container.textContent).toBe("newer chart");
  });

  it("exports on the theme background and restores the transparent one", async () => {
    const container = mountContainer();
    const spec = { ...BAR_SPEC, mark: { type: "bar", href: "https://example.com/details" } };
    const chart = await renderVegaLiteChart(container, spec, () => COLORS);

    const [first, second] = await Promise.all([chart.toBlob("svg"), chart.toBlob("svg")]);

    for (const svg of await Promise.all([first.text(), second.text()])) {
      expect(svg).toContain(COLORS.background);
      expect(svg).not.toContain("example.com");
    }
    expect(container.innerHTML).not.toContain(COLORS.background);
    chart.finalize();
  });
});

describe("clearRemoteSceneValues", () => {
  it("clears paints and cursors that load files and keeps the chart's own references", () => {
    const item = {
      fill: ["URL(https://e.test/p.svg#g)"],
      stroke: "\\55rl('//e.test/s.svg')",
      cursor: "url(c.png), auto",
      blend: "multiply; fill: red",
    };
    const remoteGradient = {
      fill: { gradient: "linear", id: "a) url(https://e.test/p.svg#g", stops: [] },
      stroke: { gradient: "linear", stops: [{ color: "url(https://e.test/s.svg)" }] },
    };
    const gradient = { gradient: "linear", id: "fade", stops: [{ color: "white" }] };
    const kept = {
      fill: "url( '#gradient_1')",
      stroke: gradient,
      cursor: "pointer",
      blend: "multiply",
    };
    const scene = { items: [{ items: [{ items: [item, remoteGradient, kept] }] }] };

    clearRemoteSceneValues(scene);

    expect(item).toEqual({ fill: null, stroke: null, cursor: null, blend: null });
    expect(remoteGradient).toEqual({ fill: null, stroke: null });
    expect(kept).toEqual({
      fill: "url( '#gradient_1')",
      stroke: gradient,
      cursor: "pointer",
      blend: "multiply",
    });
    expect(hadRemoteSceneValues(scene)).toBe(true);
  });

  it("leaves a scene with nothing to clear unmarked", () => {
    const scene = { items: [{ fill: "steelblue", stroke: "#333", cursor: "pointer" }] };

    clearRemoteSceneValues(scene);

    expect(hadRemoteSceneValues(scene)).toBe(false);
  });
});

describe("blockedLoader", () => {
  it("refuses every load and gives links no target", async () => {
    const loader = blockedLoader();

    await expect(loader.load("https://example.com/data.csv")).rejects.toThrow(/inline data/);
    await expect(
      loader.sanitize("https://example.com/a.png", { context: "image" }),
    ).rejects.toThrow();
    await expect(loader.sanitize("https://example.com", { context: "href" })).resolves.toEqual({});
    await expect(loader.http("https://example.com", {})).rejects.toThrow();
    await expect(loader.file("/etc/passwd")).rejects.toThrow();
  });
});

describe("chartThemeConfig", () => {
  it("draws text and axes in the theme colors on a transparent background", () => {
    const config = chartThemeConfig(COLORS);

    expect(config.background).toBe("transparent");
    expect(config.font).toBe("Inter");
    expect(config.axis).toMatchObject({
      labelColor: COLORS.mutedForeground,
      titleColor: COLORS.foreground,
      gridColor: COLORS.border,
    });
    expect(config.legend).toMatchObject({ labelColor: COLORS.mutedForeground });
  });
});

describe("withContainerWidth", () => {
  it("fills the container for a single view without a width", () => {
    expect(withContainerWidth(BAR_SPEC).width).toBe("container");
  });

  it("keeps an explicit width and composite layouts", () => {
    expect(withContainerWidth({ ...BAR_SPEC, width: 300 }).width).toBe(300);
    expect(withContainerWidth({ hconcat: [BAR_SPEC, BAR_SPEC] }).width).toBeUndefined();
  });
});

describe("createChartTooltip", () => {
  const event = new MouseEvent("mousemove", { clientX: 20, clientY: 30 });

  it("shows fields as text, never as markup or images", () => {
    const tooltip = createChartTooltip();

    tooltip.handler(null, event, {} as never, {
      title: "<b>Region</b>",
      amount: 5,
      image: "https://example.com/tracker.png",
    });

    const element = document.querySelector('[data-testid="chart-tooltip"]');
    expect(element?.textContent).toContain("<b>Region</b>");
    expect(element?.textContent).toContain("amount5");
    expect(element?.querySelector("b, img")).toBeNull();
    expect(element?.textContent).not.toContain("example.com");
  });

  it("hides on an empty value and removes its element", () => {
    const tooltip = createChartTooltip();
    tooltip.handler(null, event, {} as never, "42");
    expect(document.querySelector('[data-testid="chart-tooltip"]')?.textContent).toBe("42");

    tooltip.handler(null, event, {} as never, null);
    expect(document.querySelector('[data-testid="chart-tooltip"]')).toBeNull();

    tooltip.handler(null, event, {} as never, ["a", 1]);
    expect(document.querySelector('[data-testid="chart-tooltip"]')?.textContent).toBe("a, 1");
    tooltip.remove();
    expect(document.querySelector('[data-testid="chart-tooltip"]')).toBeNull();
  });
});
