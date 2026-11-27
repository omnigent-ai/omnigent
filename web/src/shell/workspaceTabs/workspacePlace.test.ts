import { describe, expect, it } from "vitest";

import { placeIdentity, type WorkspacePlace } from "./workspacePlace";

describe("placeIdentity", () => {
  it.each<[WorkspacePlace, string | null]>([
    [{ kind: "new" }, null],
    [{ kind: "new", app: "terminal" }, null],
    [{ kind: "files" }, "files"],
    [{ kind: "files", path: "a.ts", line: { start: 3 } }, "files:a.ts"],
    [{ kind: "changes", path: "a.ts" }, "changes:session"],
    [{ kind: "changes", path: "a.ts", fileTab: true }, "changes:session:a.ts"],
    [
      { kind: "changes", path: "a.ts", fileTab: true, range: { base: "main" } },
      "changes:main..:a.ts",
    ],
    [{ kind: "changes", range: { base: "main" } }, "changes:main.."],
    [{ kind: "changes", range: { base: "main", head: "HEAD" } }, "changes:main..HEAD"],
    [{ kind: "pulls" }, "pulls"],
    [
      { kind: "pull", url: "https://github.com/o/r/pull/1", view: "commits" },
      "pull:https://github.com/o/r/pull/1",
    ],
    [
      { kind: "pull", url: "https://github.com/o/r/pull/1", view: "commits", list: true },
      "pull-list:https://github.com/o/r/pull/1",
    ],
    [{ kind: "terminal", key: "k" }, "terminal:k"],
    [{ kind: "agents" }, "agents"],
    [{ kind: "tasks" }, "tasks"],
    [{ kind: "agent", id: "a" }, "agent:a"],
    [{ kind: "chat" }, null],
    [{ kind: "chat", id: "c" }, "chat:c"],
    [{ kind: "search", query: "q" }, "search"],
    [{ kind: "web", url: "https://example.com/" }, null],
  ])("%o → %s", (place, identity) => {
    expect(placeIdentity(place)).toBe(identity);
  });
});
