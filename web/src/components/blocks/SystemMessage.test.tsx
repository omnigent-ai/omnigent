import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SystemMessageView } from "./SystemMessage";

afterEach(cleanup);

describe("SystemMessageView", () => {
  it.each(["subagent_wake", "session_resumed"] as const)("hides %s control notices", (kind) => {
    const { container } = render(
      <SystemMessageView
        message={{
          kind,
          label: "Sub-agent result ready",
          body: "",
        }}
      />,
    );

    expect(screen.queryByTestId("system-message")).toBeNull();
    expect(container.textContent).toBe("");
  });
});
