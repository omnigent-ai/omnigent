import { readFileSync } from "node:fs";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SpreadsheetPreview } from "./SpreadsheetPreview";

afterEach(cleanup);

/** A fixture as the server sends a binary file: base64. */
function base64Fixture(name: string): string {
  return readFileSync(`src/shell/__fixtures__/${name}`).toString("base64");
}

function renderSpreadsheet(content: string, path: string, truncated = false) {
  return render(<SpreadsheetPreview content={content} path={path} truncated={truncated} />);
}

describe("SpreadsheetPreview", () => {
  it("shows a workbook's sheets as tabs, each its own table", async () => {
    renderSpreadsheet(base64Fixture("sales.xlsx"), "out/sales.xlsx");

    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Sales", "Notes", "Empty"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("table", { name: "out/sales.xlsx, sheet Sales" })).toBeInTheDocument();
    expect(screen.getByTestId("data-table-summary")).toHaveTextContent(
      "3 rows · 6 columns · Excel workbook",
    );

    fireEvent.click(screen.getByRole("tab", { name: "Notes" }));
    expect(screen.getByRole("cell", { name: "café, crème" })).toBeInTheDocument();
  });

  it("moves between sheets with the arrow keys and shows an empty sheet as such", async () => {
    renderSpreadsheet(base64Fixture("sales.xlsx"), "sales.xlsx");
    const sales = await screen.findByRole("tab", { name: "Sales" });
    sales.focus();

    fireEvent.keyDown(sales, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Empty" })).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Empty" }));
    expect(screen.getByText("This sheet is empty.")).toBeInTheDocument();
  });

  it("shows a Parquet file as one table, with no tabs", async () => {
    renderSpreadsheet(base64Fixture("sales.snappy.parquet"), "data/sales.parquet");

    expect(await screen.findByRole("table", { name: "data/sales.parquet" })).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByTestId("data-table-summary")).toHaveTextContent(
      "3 rows · 7 columns · Parquet",
    );
  });

  it.each([
    [
      "a compression it can't read",
      base64Fixture("sales.zstd.parquet"),
      "x.parquet",
      false,
      "This file is compressed with ZSTD, which the preview can't read yet.",
    ],
    [
      "a damaged workbook",
      btoa("not a zip"),
      "x.xlsx",
      false,
      "This file isn't a readable Excel workbook.",
    ],
    [
      "a sheet past the size limit",
      base64Fixture("understated.xlsx"),
      "x.xlsx",
      false,
      "This sheet is too large to preview. Download the workbook to open it.",
    ],
    // Not even base64: no reader names the failure, so the generic message shows.
    [
      "a file it can't decode",
      "%%% not base64 %%%",
      "x.xlsx",
      false,
      "This file couldn't be read. Download it to open it elsewhere.",
    ],
    [
      "a file past the preview limit",
      base64Fixture("sales.xlsx").slice(0, 400),
      "x.xlsx",
      true,
      "This file is larger than the preview limit",
    ],
  ])("explains %s", async (_, content, path, truncated, message) => {
    renderSpreadsheet(content, path, truncated);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("table")).toBeNull();
  });
});
