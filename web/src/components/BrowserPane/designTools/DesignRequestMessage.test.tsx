import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DesignRequestBody } from "./LazyDesignRequestMessage";
import { NO_SCREENSHOT_NOTE } from "./parseDesignRequest";

describe("DesignRequestBody", () => {
  it("shows the request, its subject and a missing screenshot's note while the card's chunk loads", async () => {
    // A fresh module, whose card hasn't loaded for an earlier test.
    vi.resetModules();
    const { DesignRequestBody: FreshBody } = await import("./LazyDesignRequestMessage");
    const { rerender } = render(
      <FreshBody
        request={{
          kind: "element",
          request: "Make it red",
          element: "p#blurb",
          noScreenshot: true,
        }}
      />,
    );
    expect(screen.queryByTestId("design-request")).toBeNull();
    expect(screen.getByText("Make it red")).toBeInTheDocument();
    expect(screen.getByText("p#blurb")).toBeInTheDocument();
    expect(screen.getByText(NO_SCREENSHOT_NOTE)).toBeInTheDocument();
    const card = await screen.findByTestId("design-request");
    expect(card).toHaveTextContent("p#blurb");
    expect(card).toHaveTextContent(NO_SCREENSHOT_NOTE);
    rerender(
      <FreshBody request={{ kind: "element", request: "Make it red", element: "p#blurb" }} />,
    );
    expect(screen.queryByText(NO_SCREENSHOT_NOTE)).toBeNull();
  });

  it("shows the request and what it was about", async () => {
    render(
      <DesignRequestBody
        request={{ kind: "element", request: "Make it red", element: "p#blurb", elementText: "Hi" }}
      />,
    );
    await screen.findByTestId("design-request");
    expect(screen.getByText('p#blurb "Hi"')).toBeInTheDocument();
    expect(screen.getByText("Make it red")).toBeInTheDocument();
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
