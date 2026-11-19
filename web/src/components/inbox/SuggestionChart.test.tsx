import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { SuggestionChart } from "./SuggestionChart";

afterEach(cleanup);

const chart = {
  version: 1,
  type: "bar",
  title: "Errors per day",
  x: ["Mon", "Tue"],
  series: [{ name: "errors", values: [3, 5] }],
  source: { label: "Error dashboard", href: "https://example.com/errors" },
};

it("renders the title and a linked source", () => {
  render(<SuggestionChart chart={chart} />);

  expect(screen.getByTestId("inbox-suggestion-chart")).toBeInTheDocument();
  expect(screen.getByText("Errors per day")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Error dashboard" })).toHaveAttribute(
    "href",
    "https://example.com/errors",
  );
});

it("renders nothing for a chart it cannot draw", () => {
  const { container } = render(<SuggestionChart chart={{ ...chart, version: 9 }} />);

  expect(container).toBeEmptyDOMElement();
});
