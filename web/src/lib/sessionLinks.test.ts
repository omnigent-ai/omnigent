import { describe, expect, it } from "vitest";

import { getSessionDeepLink, getShareableSessionLink } from "./sessionLinks";

const rebase = (path: string) => `/mount${path}`;

describe("sessionLinks", () => {
  it("keeps whole-session links on the interactive chat route", () => {
    expect(getSessionDeepLink("conv a", rebase)).toMatch(/^omnigent:\/\/[^/]+\/c\/conv%20a$/);
    expect(getShareableSessionLink("conv a", rebase)).toMatch(/\/mount\/c\/conv%20a$/);
  });

  it("routes response links through the read-only archive locator", () => {
    expect(
      getSessionDeepLink("conv_a", rebase, {
        kind: "response",
        responseId: "resp/1",
        itemId: "msg 1",
      }),
    ).toMatch(/^omnigent:\/\/[^/]+\/archive\/conv_a\?response=resp%2F1&item=msg\+1$/);
  });
});
