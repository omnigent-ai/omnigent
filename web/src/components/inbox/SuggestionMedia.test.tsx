import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { SuggestionMedia } from "./SuggestionMedia";

afterEach(cleanup);

const chart = {
  version: 1,
  type: "line",
  title: "Failures",
  x: ["a", "b"],
  series: [{ name: "n", values: [1, 2] }],
};

it("draws chart blocks and skips types this build does not know", () => {
  render(
    <SuggestionMedia
      media={[
        { type: "svg", svg: "<svg/>" },
        { type: "chart", chart },
      ]}
    />,
  );

  expect(screen.getAllByTestId("inbox-suggestion-chart")).toHaveLength(1);
  expect(screen.queryByText("<svg/>")).toBeNull();
});
