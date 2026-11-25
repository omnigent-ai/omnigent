// Which mounted list a kept toast's Undo or Try Again runs through: the newest one, or, where two
// share a scope, the newest one that lists the PR.

import { describe, expect, it } from "vitest";

import { createLiveScopes } from "./liveScope";

interface List {
  name: string;
  urls: string[];
}

const listing = (url: string) => (list: List) => list.urls.includes(url);

describe("live scopes", () => {
  it("runs through the newest mounted list, and none once each has gone", () => {
    const scopes = createLiveScopes<List>();
    const table = scopes.publish("page", { current: { name: "table", urls: [] } });
    const beside = scopes.publish("page", { current: { name: "beside", urls: [] } });
    expect(scopes.current("page")?.name).toBe("beside");
    beside();
    expect(scopes.current("page")?.name).toBe("table");
    table();
    expect(scopes.current("page")).toBeNull();
    expect(scopes.currentFor("page", () => true)).toBeNull();
  });

  it("runs a PR's recovery through the list showing it, whichever mounted last", () => {
    const scopes = createLiveScopes<List>();
    const table = { current: { name: "table", urls: ["https://github.com/acme/app/pull/1"] } };
    const beside = { current: { name: "beside", urls: ["https://github.com/acme/app/pull/2"] } };
    const removeTable = scopes.publish("page", table);
    const removeBeside = scopes.publish("page", beside);
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/1"))?.name).toBe(
      "table",
    );
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/2"))?.name).toBe(
      "beside",
    );
    // A PR neither lists goes through the newest, as one list alone would take it.
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/3"))?.name).toBe(
      "beside",
    );

    // The table remounts on a tab change: still the list that shows the PR, not the newest.
    removeTable();
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/1"))?.name).toBe(
      "beside",
    );
    const restored = scopes.publish("page", table);
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/2"))?.name).toBe(
      "beside",
    );
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/1"))?.name).toBe(
      "table",
    );

    // What a list shows is read as the PR's recovery runs, not when it mounted.
    beside.current = { ...beside.current, urls: ["https://github.com/acme/app/pull/1"] };
    table.current = { ...table.current, urls: [] };
    expect(scopes.currentFor("page", listing("https://github.com/acme/app/pull/1"))?.name).toBe(
      "beside",
    );
    restored();
    removeBeside();
  });

  it("keeps scopes apart", () => {
    const scopes = createLiveScopes<List>();
    const remove = scopes.publish("page", { current: { name: "table", urls: [] } });
    expect(scopes.currentFor("session", () => true)).toBeNull();
    remove();
  });
});
