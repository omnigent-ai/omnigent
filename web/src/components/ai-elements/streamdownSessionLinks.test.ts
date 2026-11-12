// Chat routes an `omnigent://<host>/c/<id>` link in the app only when it names the
// server the page is served from; any other link keeps its deep-link href.

import { describe, expect, it } from "vitest";
import { parseOmnigentSessionLink, sameServerSessionPath } from "./streamdown-security";

const httpsPage = { protocol: "https:", host: "example.test" };
const httpPage = { protocol: "http:", host: "127.0.0.1:53694" };

describe("sameServerSessionPath", () => {
  it.each([
    ["the same host", "omnigent://example.test/c/worker_1", httpsPage],
    ["the HTTPS default port", "omnigent://example.test:443/c/worker_1", httpsPage],
    ["a differently cased host", "omnigent://EXAMPLE.test/c/worker_1", httpsPage],
    ["the same host and port", "omnigent://127.0.0.1:53694/c/worker_1", httpPage],
    [
      "the HTTP default port",
      "omnigent://localhost:80/c/worker_1",
      { protocol: "http:", host: "localhost" },
    ],
  ])("routes a link with %s in the app", (_case, href, page) => {
    expect(sameServerSessionPath(href, page)).toBe("/c/worker_1");
  });

  it.each([
    ["another host", "omnigent://other.example/c/worker_1", httpsPage],
    ["another port", "omnigent://127.0.0.1:53695/c/worker_1", httpPage],
    ["the HTTP default port on an HTTPS page", "omnigent://example.test:80/c/worker_1", httpsPage],
    [
      "a page that isn't served over HTTP",
      "omnigent://example.test/c/worker_1",
      { protocol: "file:", host: "example.test" },
    ],
    ["a link that isn't a session link", "omnigent://example.test/settings", httpsPage],
  ])("keeps the deep link for %s", (_case, href, page) => {
    expect(sameServerSessionPath(href, page)).toBeNull();
  });
});

describe("parseOmnigentSessionLink", () => {
  it.each([
    ["a bracketed host that isn't an address", "omnigent://[.]/c/abc"],
    ["an out-of-range port", "omnigent://127.0.0.1:99999/c/abc"],
  ])("rejects %s", (_case, href) => {
    expect(parseOmnigentSessionLink(href)).toBeNull();
  });

  it("accepts an IPv6 host", () => {
    expect(parseOmnigentSessionLink("omnigent://[::1]:53694/c/abc")).toEqual({
      host: "[::1]:53694",
      sessionId: "abc",
    });
  });
});
