import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DesignRequestBody } from "./LazyDesignRequestMessage";
import { NO_SCREENSHOT_NOTE } from "./parseDesignRequest";

describe("DesignRequestBody", () => {
  it("shows the request and its subject while the card's chunk loads", async () => {
    render(
      <DesignRequestBody
        request={{ kind: "element", request: "Make it red", element: "p#blurb" }}
      />,
    );
    expect(screen.getByText("Make it red")).toBeInTheDocument();
    expect(screen.getByText("p#blurb")).toBeInTheDocument();
    expect(screen.queryByTestId("design-request")).toBeNull();
    expect(await screen.findByTestId("design-request")).toHaveTextContent("p#blurb");
  });

  it("shows the request and what it was about", async () => {
    render(
      <DesignRequestBody
        request={{ kind: "element", request: "Make it red", element: "p#blurb", elementText: "Hi" }}
      />,
    );
    expect(await screen.findByText('p#blurb "Hi"')).toBeInTheDocument();
    expect(screen.getByText("Make it red")).toBeInTheDocument();
  });

  it("says which page a drawing was on", async () => {
    render(
      <DesignRequestBody
        request={{ kind: "drawing", request: "Bigger heading", page: "localhost:5173" }}
      />,
    );
    expect(await screen.findByText("Drawing on localhost:5173")).toBeInTheDocument();
    expect(screen.getByText("Bigger heading")).toBeInTheDocument();
  });

  it("notes a request the agent got without its screenshot, before and after the chunk", async () => {
    const { rerender } = render(
      <DesignRequestBody
        request={{ kind: "element", request: "Red", element: "p", noScreenshot: true }}
      />,
    );
    expect(screen.getByText(NO_SCREENSHOT_NOTE)).toBeInTheDocument();
    expect(await screen.findByTestId("design-request")).toHaveTextContent(NO_SCREENSHOT_NOTE);
    rerender(<DesignRequestBody request={{ kind: "element", request: "Red", element: "p" }} />);
    expect(screen.queryByText(NO_SCREENSHOT_NOTE)).toBeNull();
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
