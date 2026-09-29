import { describe, expect, it } from "vitest";

import { CLIENT_CREATE_TOKEN_LABEL, isTempConvId, newTempConversation } from "./tempConversationId";

describe("temporary conversation ids", () => {
  it("mints a UUID-derived lowercase-hex token for a client-only temp route", () => {
    const first = newTempConversation();
    const second = newTempConversation();

    expect(first.token).toMatch(/^[0-9a-f]{32}$/);
    expect(first.id).toBe(`temp:${first.token}`);
    expect(second.token).not.toBe(first.token);
    expect(CLIENT_CREATE_TOKEN_LABEL).toBe("omnigent.client_create_token");
  });

  it("recognizes temporary ids by prefix without prior registration", () => {
    expect(isTempConvId("temp:0123456789abcdef0123456789abcdef")).toBe(true);
    expect(isTempConvId("conv_0123456789abcdef0123456789abcdef")).toBe(false);
    expect(isTempConvId(null)).toBe(false);
  });
});
