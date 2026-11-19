import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DesignOutboxStrip } from "./DesignOutboxStrip";
import { resetDesignOutbox, useDesignOutbox, type DesignRequest } from "./core/designOutbox";
import { DesignOutboxStrip as LazyStrip } from "./LazyDesignOutboxStrip";

const request = (overrides: Partial<DesignRequest>): DesignRequest => ({
  id: "r1",
  tabId: "browser-tab:conv_1:tab_1",
  conversationId: "conv_1",
  label: "Annotate button",
  text: "Green",
  files: [],
  status: "waiting",
  ...overrides,
});

afterEach(() => {
  resetDesignOutbox();
});

describe("DesignOutboxStrip", () => {
  it("lists the session's requests from any of its tabs, even a closed one", () => {
    useDesignOutbox.setState({
      requests: [
        request({ id: "a", tabId: "browser-tab:conv_1:closed", label: "Draw: bigger" }),
        request({ id: "b", label: "Annotate button", status: "sending" }),
        request({ id: "c", conversationId: "conv_2", label: "Elsewhere" }),
        request({ id: "d", label: "Already sent", status: "sent" }),
      ],
    });
    render(<DesignOutboxStrip conversationId="conv_1" />);
    expect(screen.getByText("Draw: bigger")).toBeInTheDocument();
    expect(screen.getByText("Waiting for the agent")).toBeInTheDocument();
    expect(screen.getByText("Sending…")).toBeInTheDocument();
    expect(screen.queryByText("Elsewhere")).toBeNull();
    expect(screen.queryByText("Already sent")).toBeNull();
    // A request that's sending may be reaching the agent: no Remove for it.
    expect(screen.queryByRole("button", { name: "Remove Annotate button" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove Draw: bigger" })).toBeInTheDocument();
  });

  it("retries a failed request and removes one", () => {
    useDesignOutbox.setState({
      requests: [
        request({ id: "f", status: "failed", error: "Couldn't send: offline" }),
        request({ id: "d", label: "Denied one", status: "denied", error: "A policy blocked it" }),
      ],
    });
    render(<DesignOutboxStrip conversationId="conv_1" />);
    expect(screen.getByText("Couldn't send: offline")).toBeInTheDocument();
    // Denied: retrying would be denied again, so only Remove.
    expect(screen.queryByRole("button", { name: "Retry Denied one" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove Denied one" }));
    expect(useDesignOutbox.getState().requests.map((item) => item.id)).toEqual(["f"]);
    fireEvent.click(screen.getByRole("button", { name: "Retry Annotate button" }));
    const retried = useDesignOutbox.getState().requests[0];
    expect(["waiting", "sending"]).toContain(retried?.status);
    expect(retried?.error).toBeUndefined();
  });

  it("shows a failure's whole text, which says what to check before retrying", () => {
    const error =
      "The server didn't answer, so it may have arrived: check the chat before retrying.";
    useDesignOutbox.setState({ requests: [request({ status: "failed", error })] });
    render(<DesignOutboxStrip conversationId="conv_1" />);
    const text = screen.getByText(error);
    expect(text).not.toHaveClass("truncate");
    expect(text.parentElement).not.toHaveClass("truncate");
    expect(text.parentElement).toHaveAttribute("title", error);
  });

  it("mounts only once the session has requests to show", async () => {
    render(<LazyStrip conversationId="conv_1" />);
    expect(screen.queryByTestId("design-outbox-strip")).toBeNull();
    await act(async () => useDesignOutbox.setState({ requests: [request({ status: "failed" })] }));
    expect(await screen.findByTestId("design-outbox-strip")).toBeInTheDocument();
    await act(async () => useDesignOutbox.setState({ requests: [] }));
    expect(screen.queryByTestId("design-outbox-strip")).toBeNull();
  });
});
