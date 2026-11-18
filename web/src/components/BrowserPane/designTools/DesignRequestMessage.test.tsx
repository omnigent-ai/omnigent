import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DesignRequestBody } from "./DesignRequestMessage";
import { NO_SCREENSHOT_NOTE } from "./parseDesignRequest";

describe("DesignRequestBody", () => {
  it("shows the request, what it was about and a missing screenshot's note", () => {
    const { rerender } = render(
      <DesignRequestBody
        request={{
          kind: "element",
          request: "Make it red",
          element: "p#blurb",
          elementText: "Hi",
          noScreenshot: true,
        }}
      />,
    );
    const card = screen.getByTestId("design-request");
    expect(card).toHaveTextContent('p#blurb "Hi"');
    expect(card).toHaveTextContent("Make it red");
    expect(card).toHaveTextContent(NO_SCREENSHOT_NOTE);
    rerender(
      <DesignRequestBody
        request={{ kind: "element", request: "Make it red", element: "p#blurb" }}
      />,
    );
    expect(screen.queryByText(NO_SCREENSHOT_NOTE)).toBeNull();
  });

  it("says which page a drawing was on", async () => {
    render(
      <DesignRequestBody
        request={{ kind: "drawing", request: "Bigger heading", page: "localhost:5173" }}
      />,
    );
    await screen.findByTestId("design-request");
    expect(screen.getByText("Drawing on localhost:5173")).toBeInTheDocument();
    expect(screen.getByText("Bigger heading")).toBeInTheDocument();
  });

  it("shows the words as the chat renders them, and a long subject in full", async () => {
    const element = `div.${"long-class-name.".repeat(10)}end`;
    render(
      <DesignRequestBody
        request={{ kind: "element", request: "Make it red", element }}
        words={<em>Make it red</em>}
      />,
    );
    const card = await screen.findByTestId("design-request");
    expect(card.querySelector("em")).toHaveTextContent("Make it red");
    expect(screen.getByText(element)).not.toHaveClass("truncate");
  });
});
