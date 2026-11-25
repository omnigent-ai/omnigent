import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const routes = vi.hoisted(() => ({ readOnMac: true, writesOnMac: false }));
vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  readsOnThisMac: () => routes.readOnMac,
}));
vi.mock("@/lib/desktopPullRequestWrites", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  macWriteDispatch: () =>
    routes.writesOnMac ? { kind: "mac", route: { target: {}, actor: {} } } : { kind: "host" },
}));

import { ActionRouteHintsContext, useActionRouteHint } from "./actionRouteHint";

const PR = "https://github.com/acme/app/pull/1";

function MergeButton() {
  return (
    <button type="button" title={useActionRouteHint(PR, "Merge")}>
      Merge
    </button>
  );
}

function renderOnPage(page: boolean, child: ReactNode = <MergeButton />) {
  render(<ActionRouteHintsContext.Provider value={page}>{child}</ActionRouteHintsContext.Provider>);
}

describe("useActionRouteHint", () => {
  it("says where a change goes on the new page when it isn't where the PR is read", () => {
    routes.readOnMac = true;
    routes.writesOnMac = false;
    renderOnPage(true);
    expect(screen.getByRole("button", { name: "Merge" })).toHaveAttribute(
      "title",
      "Merge through the session’s machine",
    );
  });

  it.each([
    ["the routes agree on this Mac", true, true],
    ["both are the session's machine", false, false],
  ])("stays quiet where %s", (_case, readOnMac, writesOnMac) => {
    routes.readOnMac = readOnMac;
    routes.writesOnMac = writesOnMac;
    renderOnPage(true);
    expect(screen.getByRole("button", { name: "Merge" })).not.toHaveAttribute("title");
  });

  it("stays off on the classic panel", () => {
    routes.readOnMac = true;
    routes.writesOnMac = false;
    renderOnPage(false);
    expect(screen.getByRole("button", { name: "Merge" })).not.toHaveAttribute("title");
  });
});
