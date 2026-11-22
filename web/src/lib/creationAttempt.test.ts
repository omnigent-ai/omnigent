import { describe, expect, it } from "vitest";
import { reuseCreationAttempt } from "./creationAttempt";

describe("creation attempt keys", () => {
  it("reuses the same operation only while submitted choices and files are unchanged", () => {
    const file = new File(["notes"], "notes.txt");
    const attempt = reuseCreationAttempt(undefined, { model: "model-a" }, [file]);
    expect(attempt.key).toMatch(/^[0-9a-f-]{36}$/);
    expect(reuseCreationAttempt(attempt, { model: "model-a" }, [file])).toBe(attempt);
    expect(reuseCreationAttempt(attempt, { model: "model-b" }, [file]).key).not.toBe(attempt.key);
    expect(
      reuseCreationAttempt(attempt, { model: "model-a" }, [new File(["notes"], "notes.txt")]).key,
    ).not.toBe(attempt.key);
    expect(reuseCreationAttempt(attempt, { model: "model-a" }).key).not.toBe(attempt.key);
  });

  it("snapshots attachment order independently of the caller's array", () => {
    const files = [new File(["one"], "one.txt"), new File(["two"], "two.txt")];
    const attempt = reuseCreationAttempt(undefined, {}, files);
    files.reverse();
    expect(attempt.files.map((file) => file.name)).toEqual(["one.txt", "two.txt"]);
    expect(reuseCreationAttempt(attempt, {}, files).key).not.toBe(attempt.key);
  });
});
