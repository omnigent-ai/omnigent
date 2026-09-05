import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { ArchiveDateRangePicker } from "./ArchiveDateRangePicker";

function Harness({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return <ArchiveDateRangePicker value={value} onValueChange={setValue} inlineCalendar />;
}

describe("ArchiveDateRangePicker", () => {
  it("keeps typed input and calendar selection in one state", () => {
    render(<Harness initial="20260902-20260905" />);

    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20260902-20260905");
    expect(screen.getByRole("button", { name: "Choose year" })).toHaveTextContent("2026");
    expect(screen.getByRole("button", { name: "Choose month" })).toHaveTextContent("September");
    expect(screen.getByRole("button", { name: "2026-09-02" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "2026-09-05" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("selects a single day first and a range on the next click", () => {
    render(<Harness initial="20260902" />);

    fireEvent.click(screen.getByRole("button", { name: "2026-09-05" }));
    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20260905");
    fireEvent.click(screen.getByRole("button", { name: "2026-09-02" }));
    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20260902-20260905");
  });

  it("lets the user jump directly through year and month without changing the filter early", () => {
    render(<Harness initial="20260903" />);

    fireEvent.click(screen.getByRole("button", { name: "Choose year" }));
    fireEvent.click(screen.getByRole("button", { name: "Choose year 2028" }));
    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20260903");
    fireEvent.click(screen.getByRole("button", { name: "Choose month" }));
    expect(screen.getByRole("button", { name: "Choose 2028-03" })).toHaveTextContent("Mar");
    fireEvent.click(screen.getByRole("button", { name: "Choose 2028-03" }));

    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20260903");
    fireEvent.click(screen.getByRole("button", { name: "2028-03-12" }));
    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20280312");
  });

  it("pages through years in batches of twelve", () => {
    render(<Harness initial="20260903" />);

    fireEvent.click(screen.getByRole("button", { name: "Choose year" }));
    expect(screen.getByRole("button", { name: "Choose year 2020" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose year 2031" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next 12 years" }));
    expect(screen.getByRole("button", { name: "Choose year 2032" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose year 2043" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Previous 12 years" }));
    expect(screen.getByRole("button", { name: "Choose year 2026" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
