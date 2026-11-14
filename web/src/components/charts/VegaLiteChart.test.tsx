import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { CSSProperties } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VegaLiteChart } from "./VegaLiteChart";

let resolvedTheme = "light";
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme }),
}));

const triggerBrowserDownload = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useFileContent", () => ({ triggerBrowserDownload }));

const BAR_SPEC = JSON.stringify({
  data: {
    values: [
      { region: "North", sales: 28 },
      { region: "South", sales: 55 },
    ],
  },
  mark: "bar",
  encoding: {
    x: { field: "region", type: "nominal" },
    y: { field: "sales", type: "quantitative" },
  },
});

afterEach(() => {
  cleanup();
  resolvedTheme = "light";
  triggerBrowserDownload.mockReset();
});

async function drawnChart(): Promise<HTMLElement> {
  const plot = screen.getByTestId("vega-lite-chart-plot");
  await waitFor(() => expect(plot.querySelectorAll("g.mark-rect path")).toHaveLength(2));
  return plot;
}

describe("VegaLiteChart", () => {
  it("draws the spec and enables downloads once drawn", async () => {
    render(<VegaLiteChart spec={BAR_SPEC} filename="sales" />);
    expect(screen.getByRole("button", { name: "Download SVG" })).toBeDisabled();

    await drawnChart();
    fireEvent.click(screen.getByRole("button", { name: "Download SVG" }));

    await waitFor(() => expect(triggerBrowserDownload).toHaveBeenCalledTimes(1));
    const [blob, filename] = triggerBrowserDownload.mock.calls[0] as [Blob, string];
    expect(filename).toBe("sales.svg");
    expect(blob.type).toBe("image/svg+xml");
    expect(await blob.text()).toContain("<svg");
  });

  it("draws in the theme colors in effect around it", async () => {
    resolvedTheme = "dark";
    const theme = { "--foreground": "#fafafa" } as CSSProperties;
    render(
      <div style={theme}>
        <VegaLiteChart spec={BAR_SPEC} />
      </div>,
    );

    const plot = await drawnChart();
    expect(plot.querySelector("g.role-axis-title text")).toHaveAttribute("fill", "#fafafa");
  });

  it("redraws in the new colors when the page theme changes", async () => {
    const root = document.documentElement;
    try {
      root.style.setProperty("--foreground", "#111111");
      render(<VegaLiteChart spec={BAR_SPEC} />);
      const plot = await drawnChart();
      expect(plot.querySelector("g.role-axis-title text")).toHaveAttribute("fill", "#111111");

      root.style.setProperty("--foreground", "#eeeeee");
      root.classList.add("dark");

      await waitFor(() =>
        expect(plot.querySelector("g.role-axis-title text")).toHaveAttribute("fill", "#eeeeee"),
      );
    } finally {
      root.classList.remove("dark");
      root.style.removeProperty("--foreground");
    }
  });

  it("shows only the latest spec when it changes mid-draw", async () => {
    const threeBars = JSON.stringify({
      ...JSON.parse(BAR_SPEC),
      data: {
        values: [
          { region: "A", sales: 1 },
          { region: "B", sales: 2 },
          { region: "C", sales: 3 },
        ],
      },
    });
    const { rerender } = render(<VegaLiteChart spec={threeBars} />);

    rerender(<VegaLiteChart spec={BAR_SPEC} />);

    const plot = await drawnChart();
    expect(plot.querySelectorAll("svg")).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("explains a refused spec and shows its source", () => {
    const spec = JSON.stringify({ data: { url: "https://example.com/data.csv" }, mark: "bar" });
    render(<VegaLiteChart spec={spec} source={<pre>spec source</pre>} />);

    expect(screen.getByRole("alert")).toHaveTextContent(/inline data/);
    expect(screen.getByText("spec source")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download PNG" })).toBeDisabled();
  });

  it("explains a spec Vega-Lite can't compile", async () => {
    render(<VegaLiteChart spec={JSON.stringify({ mark: "nope", data: { values: [] } })} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Couldn't draw this chart/);
  });

  it("draws a corrected spec after an error", async () => {
    const { rerender } = render(<VegaLiteChart spec="{" />);
    expect(screen.getByRole("alert")).toHaveTextContent(/valid JSON/);

    rerender(<VegaLiteChart spec={BAR_SPEC} />);

    await drawnChart();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("toggles between the chart and its spec", async () => {
    render(<VegaLiteChart spec={BAR_SPEC} source={<pre>spec source</pre>} />);
    await drawnChart();
    expect(screen.queryByText("spec source")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Spec/ }));

    expect(screen.getByText("spec source")).toBeInTheDocument();
    expect(screen.getByTestId("vega-lite-chart-plot").parentElement).toHaveClass("hidden");
  });
});
