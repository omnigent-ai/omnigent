import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { ArchiveDateRangePicker, type ArchiveDatePreset } from "./ArchiveDateRangePicker";

function Harness({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return <ArchiveDateRangePicker value={value} onValueChange={setValue} inlineCalendar />;
}

function PresetHarness({ initialPreset = "lt30d" }: { initialPreset?: ArchiveDatePreset }) {
  const [value, setValue] = useState("");
  const [preset, setPreset] = useState<ArchiveDatePreset>(initialPreset);
  return (
    <>
      <ArchiveDateRangePicker
        value={value}
        onValueChange={setValue}
        agePreset={preset}
        onAgePresetChange={setPreset}
        inlineCalendar
      />
      <output data-testid="committed-date-range">{value}</output>
    </>
  );
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

  it("keeps the input empty while the default <30d preset is active", () => {
    render(<PresetHarness />);

    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Last 30 days" })).toHaveTextContent("<30d");
    expect(screen.getByRole("button", { name: "Last 30 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("uses the query cutoff reference for preset calendar highlighting", () => {
    const reference = Math.floor(new Date(2033, 4, 18, 12).getTime() / 1000);
    render(
      <ArchiveDateRangePicker
        value=""
        onValueChange={() => undefined}
        agePreset="lt7d"
        ageReferenceSeconds={reference}
        onAgePresetChange={() => undefined}
        inlineCalendar
      />,
    );

    expect(screen.getByRole("button", { name: "Choose year" })).toHaveTextContent("2033");
    expect(screen.getByRole("button", { name: "Choose month" })).toHaveTextContent("May");
    expect(screen.getByRole("button", { name: "2033-05-18" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("lets a valid manual range override the active preset", () => {
    render(<PresetHarness />);

    fireEvent.change(screen.getByLabelText("Archive day or date range"), {
      target: { value: "20260901-20260904" },
    });

    expect(screen.getByRole("button", { name: "Last 30 days" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20260901-20260904");
  });

  it("keeps the preset active until the manual input becomes valid", () => {
    render(<PresetHarness />);

    fireEvent.change(screen.getByLabelText("Archive day or date range"), {
      target: { value: "202609" },
    });

    expect(screen.getByRole("button", { name: "Last 30 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("keeps the last valid filter while a longer range is still being typed", () => {
    render(<PresetHarness />);
    const input = screen.getByLabelText("Archive day or date range");

    fireEvent.change(input, { target: { value: "20260901" } });
    expect(screen.getByTestId("committed-date-range")).toHaveTextContent("20260901");

    fireEvent.change(input, { target: { value: "20260901-" } });
    expect(input).toHaveValue("20260901-");
    expect(screen.getByTestId("committed-date-range")).toHaveTextContent(/^20260901$/);

    fireEvent.change(input, { target: { value: "20260901-20260904" } });
    expect(screen.getByTestId("committed-date-range")).toHaveTextContent("20260901-20260904");
  });

  it("clears the manual input when a preset is selected again", () => {
    render(<PresetHarness />);
    fireEvent.change(screen.getByLabelText("Archive day or date range"), {
      target: { value: "20260901-20260904" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Last 7 days" }));

    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Last 7 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("returns the calendar to the current month without changing the filter", () => {
    render(<Harness initial="20240115" />);

    fireEvent.click(screen.getByRole("button", { name: "Today" }));

    const currentMonth = new Date().toLocaleDateString("en-US", { month: "long" });
    expect(screen.getByRole("button", { name: "Choose year" })).toHaveTextContent(
      String(new Date().getFullYear()),
    );
    expect(screen.getByRole("button", { name: "Choose month" })).toHaveTextContent(currentMonth);
    expect(screen.getByLabelText("Archive day or date range")).toHaveValue("20240115");
  });
});
