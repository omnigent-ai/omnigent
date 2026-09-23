import { afterEach, describe, expect, it } from "vitest";
import { MESSAGE_QUERY_PARAM, buildMessageDeepLink, findMessageElement } from "./messageDeepLink";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("buildMessageDeepLink", () => {
  it("sets ?message= on the session URL and preserves other params", () => {
    const link = buildMessageDeepLink(
      "msg_abc",
      "https://app.example/c/conv_1?debug=1&file=README.md",
    );
    const url = new URL(link);
    expect(url.pathname).toBe("/c/conv_1");
    expect(url.searchParams.get(MESSAGE_QUERY_PARAM)).toBe("msg_abc");
    expect(url.searchParams.get("debug")).toBe("1");
    expect(url.searchParams.get("file")).toBe("README.md");
  });

  it("replaces an existing message param", () => {
    const link = buildMessageDeepLink("msg_new", "https://app.example/c/conv_1?message=msg_old");
    expect(new URL(link).searchParams.get(MESSAGE_QUERY_PARAM)).toBe("msg_new");
  });
});

describe("findMessageElement", () => {
  it("returns the element stamped with data-message-id", () => {
    document.body.innerHTML = `<div data-message-id="m1">hi</div>`;
    expect(findMessageElement("m1")?.textContent).toBe("hi");
    expect(findMessageElement("missing")).toBeNull();
  });
});
