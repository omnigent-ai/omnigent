import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DataTablePreview } from "./DataTablePreview";
import { DATA_TABLE_MAX_COLUMNS, DATA_TABLE_MAX_ROWS } from "./dataTable";

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
});

/** Replace the clipboard for one test; ``afterEach`` puts the original back. */
function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
}

const SALES = "region,units,closed\nWest,10,2026-03-01\nEast,2,2026-01-15\nNorth,,2026-02-10\n";

function renderTable(content: string, opts: { path?: string; truncated?: boolean } = {}) {
  return render(
    <DataTablePreview
      content={content}
      encoding="utf-8"
      path={opts.path ?? "sales.csv"}
      truncated={opts.truncated ?? false}
    />,
  );
}

/** The table's body cells in *column*, top to bottom. */
function columnValues(column: number): string[] {
  const body = screen.getByRole("table").querySelector("tbody")!;
  return within(body)
    .getAllByRole("row")
    .map((row) => within(row).getAllByRole("cell")[column].textContent ?? "");
}

describe("DataTablePreview", () => {
  it("shows the header, each column's type and a summary", () => {
    renderTable(SALES);
    const headers = screen.getAllByRole("columnheader");
    expect(headers.map((header) => header.textContent)).toEqual([
      expect.stringContaining("region"),
      expect.stringContaining("units"),
      expect.stringContaining("closed"),
    ]);
    expect(
      headers.map((header) => within(header).getByText(/^(text|number|date)$/).textContent),
    ).toEqual(["text", "number", "date"]);
    expect(screen.getByTestId("data-table-summary")).toHaveTextContent(
      "3 rows · 3 columns · comma-separated",
    );
    expect(columnValues(0)).toEqual(["West", "East", "North"]);
  });

  it("sorts by a column, ascending then descending then back to file order", () => {
    renderTable(SALES);
    const units = screen.getByRole("button", { name: /units/ });

    fireEvent.click(units);
    expect(columnValues(1)).toEqual(["2", "10", ""]);
    expect(screen.getAllByRole("columnheader")[1]).toHaveAttribute("aria-sort", "ascending");

    fireEvent.click(units);
    // Empty cells stay last either way.
    expect(columnValues(1)).toEqual(["10", "2", ""]);

    fireEvent.click(units);
    expect(columnValues(0)).toEqual(["West", "East", "North"]);
    expect(screen.getAllByRole("columnheader")[1]).toHaveAttribute("aria-sort", "none");
  });

  it("filters a column by the text it contains, ignoring case, across columns at once", () => {
    renderTable(SALES);
    fireEvent.change(screen.getByRole("textbox", { name: "Filter region" }), {
      target: { value: "ST" },
    });
    expect(columnValues(0)).toEqual(["West", "East"]);
    expect(screen.getByText("2 matching")).toBeInTheDocument();
    // A second filter narrows the rows the first one kept.
    fireEvent.change(screen.getByRole("textbox", { name: "Filter units" }), {
      target: { value: "2" },
    });
    expect(columnValues(0)).toEqual(["East"]);
    expect(screen.getByText("1 matching")).toBeInTheDocument();
  });

  it("copies the selected cell or its row", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    renderTable(SALES);
    expect(screen.queryByRole("button", { name: /Copy cell/ })).toBeNull();

    fireEvent.click(screen.getByRole("cell", { name: "East" }));
    fireEvent.click(screen.getByRole("button", { name: /Copy cell/ }));
    fireEvent.click(screen.getByRole("button", { name: /Copy row/ }));

    await vi.waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
    expect(writeText).toHaveBeenNthCalledWith(1, "East");
    expect(writeText).toHaveBeenNthCalledWith(2, "East\t2\t2026-01-15");
    await vi.waitFor(() => expect(toast.success).toHaveBeenCalledWith("Copied row"));
  });

  it("says so when no way of copying works", async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error("denied")));
    // jsdom has no execCommand, so the fallback fails too.
    renderTable(SALES);
    fireEvent.click(screen.getByRole("cell", { name: "East" }));
    fireEvent.click(screen.getByRole("button", { name: /Copy cell/ }));
    await vi.waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn't copy the cell"));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("says how many columns it shows when the file has more", () => {
    const header = Array.from({ length: DATA_TABLE_MAX_COLUMNS + 100 }, (_, i) => `c${i}`);
    renderTable(`${header.join(",")}\n1\n`);
    expect(screen.getByTestId("data-table-summary")).toHaveTextContent("1 row · 600 columns");
    expect(screen.getAllByRole("columnheader")).toHaveLength(DATA_TABLE_MAX_COLUMNS);
    expect(screen.getByRole("note")).toHaveTextContent(
      "Showing the first 500 of 600 columns. Download the file to see all of it.",
    );
  });

  it("says how many rows it shows when the file has more", () => {
    const rows = Array.from({ length: DATA_TABLE_MAX_ROWS + 500 }, (_, i) => `${i}`);
    renderTable(["id", ...rows].join("\n"));
    expect(screen.getByRole("note")).toHaveTextContent(
      "Showing the first 1,000 of 1,500 rows. Download the file to see every row.",
    );
    expect(columnValues(0)).toHaveLength(DATA_TABLE_MAX_ROWS);
  });

  it("gives only a lower bound when the server sent part of the file", () => {
    renderTable(SALES, { truncated: true });
    expect(screen.getByTestId("data-table-summary")).toHaveTextContent("At least 3 rows");
    // Every row it has is shown; the viewer's own banner says the file is cut short.
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("starts afresh when the file is rewritten with other columns", () => {
    const view = renderTable(SALES);
    fireEvent.click(screen.getByRole("button", { name: "Sort by closed" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Filter closed" }), {
      target: { value: "2026" },
    });

    view.rerender(
      <DataTablePreview
        content={"region\nWest\nEast\n"}
        encoding="utf-8"
        path="sales.csv"
        truncated={false}
      />,
    );

    expect(columnValues(0)).toEqual(["West", "East"]);
    expect(screen.getAllByRole("columnheader")[0]).toHaveAttribute("aria-sort", "none");
  });

  it("selects a cell from the keyboard and copies its row as quoted tab-separated text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    renderTable('name,note\nLee,"tab\there and\na ""quote"""\n');

    fireEvent.focus(screen.getByRole("cell", { name: "Lee" }));
    fireEvent.click(screen.getByRole("button", { name: /Copy row/ }));

    await vi.waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('Lee\t"tab\there and\na ""quote"""'),
    );
  });

  it("is one tab stop, moved around by the arrow keys", () => {
    renderTable(SALES);
    const tabStops = () =>
      screen.getAllByRole("cell").filter((cell) => cell.getAttribute("tabindex") === "0");
    expect(tabStops().map((cell) => cell.textContent)).toEqual(["West"]);

    const west = screen.getByRole("cell", { name: "West" });
    west.focus();
    const press = (key: string) => fireEvent.keyDown(document.activeElement!, { key });
    // At the first row and column, Up and Left stay put.
    press("ArrowUp");
    press("ArrowLeft");
    expect(document.activeElement?.textContent).toBe("West");
    press("ArrowDown");
    expect(document.activeElement?.textContent).toBe("East");
    press("ArrowRight");
    expect(document.activeElement?.textContent).toBe("2");
    press("End");
    expect(document.activeElement?.textContent).toBe("2026-01-15");
    // At the last column, Right stays put; Home goes back to the first.
    press("ArrowRight");
    expect(document.activeElement?.textContent).toBe("2026-01-15");
    expect(tabStops().map((cell) => cell.textContent)).toEqual(["2026-01-15"]);
    expect(screen.getByRole("button", { name: /Copy cell/ })).toBeInTheDocument();
    press("Home");
    expect(document.activeElement?.textContent).toBe("East");
    // At the last row, Down stays put; Up moves back.
    press("ArrowDown");
    press("ArrowDown");
    expect(document.activeElement?.textContent).toBe("North");
    press("ArrowUp");
    expect(document.activeElement?.textContent).toBe("East");
  });

  it("resizes a column from the keyboard", () => {
    renderTable(SALES);
    const handle = screen.getByRole("separator", { name: "Resize units" });
    const header = screen.getAllByRole("columnheader")[1];
    expect(header.style.width).toBe("");
    // Before a resize the handle reports the rendered width (no layout in jsdom: the minimum).
    fireEvent.focus(handle);
    expect(handle).toHaveAttribute("aria-valuenow", "64");
    expect(handle).toHaveAttribute("aria-valuetext", "Fits its content");

    // jsdom has no layout, so the first step starts from zero and clamps to the minimum.
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(header.style.width).toBe("64px");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(header.style.width).toBe("80px");
    // Assistive technology hears the width and its bounds.
    expect(handle).toHaveAttribute("aria-valuenow", "80");
    expect(handle).toHaveAttribute("aria-valuetext", "80 pixels");
    expect(handle).toHaveAttribute("aria-valuemin", "64");
    expect(handle).toHaveAttribute("aria-valuemax", "2048");
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(header.style.width).toBe("64px");
  });

  it("reads a tab-separated file by its extension", () => {
    renderTable("a,b\tc\n1,2\t3\n", { path: "data.tsv" });
    expect(screen.getByTestId("data-table-summary")).toHaveTextContent(
      "1 row · 2 columns · tab-separated",
    );
    expect(columnValues(0)).toEqual(["1,2"]);
  });

  it("points an empty file at the source view", () => {
    renderTable("");
    expect(screen.getByText(/This file has no rows/)).toBeInTheDocument();
  });
});
