import { describe, expect, it } from "vitest";

import { splitSessionMessageOrigin } from "./sessionMessageOrigin";

describe("splitSessionMessageOrigin", () => {
  it("splits the sender id off a sys_session_message delivery", () => {
    expect(
      splitSessionMessageOrigin(
        "[Message from session 9b8b26c8edfd40d781c0d13a04fa5c0b]\nhi\nthere",
      ),
    ).toEqual({ sourceId: "9b8b26c8edfd40d781c0d13a04fa5c0b", body: "hi\nthere" });
  });

  it("ignores the prefix anywhere but the start, or without its newline", () => {
    expect(splitSessionMessageOrigin("hi [Message from session abc]\nthere")).toBeNull();
    expect(splitSessionMessageOrigin("[Message from session abc] hi")).toBeNull();
    expect(splitSessionMessageOrigin("plain text")).toBeNull();
  });
});
