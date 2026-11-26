import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProjectHomeTabs } from "./ProjectSummary";

afterEach(cleanup);

describe("ProjectHomeTabs", () => {
  it("flags mandate proposals waiting on the viewer on the Rules tab", () => {
    const { rerender } = render(<ProjectHomeTabs view="board" onChange={() => {}} rulesBadge={2} />);
    expect(screen.getByRole("tab", { name: "Rules, 2 proposed mandate changes" })).toBeVisible();
    rerender(<ProjectHomeTabs view="board" onChange={() => {}} />);
    expect(screen.getByRole("tab", { name: "Rules" })).toBeVisible();
    expect(screen.queryByTestId("project-rules-tab-badge")).toBeNull();
  });
});
