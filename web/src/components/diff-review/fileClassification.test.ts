import { describe, expect, it } from "vitest";
import { isGeneratedPath, isTestPath, testFileMatcher } from "./fileClassification";

describe("test file classification", () => {
  it.each([
    "src/app/Foo.test.ts",
    "web/src/Button.spec.tsx",
    "server/handler_test.go",
    "omnigent/tests/test_runner.py",
    "pkg/tests.py",
    "core/src/FooSuite.scala",
    "Sources/AppTests.swift",
    "spec/model_spec.rb",
    "tests/integration.rs",
    "crates/core/tests/parse.rs",
  ])("recognizes %s", (path) => {
    expect(isTestPath(path)).toBe(true);
  });

  it.each([
    "src/app/Foo.ts",
    "web/src/testing.ts",
    "docs/test-plan.md",
    "src/tests/helpers.ts",
    "crates/core/src/tests.rs",
  ])("leaves %s alone", (path) => {
    expect(isTestPath(path)).toBe(false);
  });

  it("matches slash patterns against the whole path and others against the name", () => {
    const matcher = testFileMatcher("checks/*.py\n*_check.py\n\n");
    expect(matcher("checks/a.py")).toBe(true);
    expect(matcher("nested/checks/a.py")).toBe(false);
    expect(matcher("nested/deep/a_check.py")).toBe(true);
  });

  it("treats regex characters in patterns literally", () => {
    const matcher = testFileMatcher("a+b(c).txt");
    expect(matcher("dir/a+b(c).txt")).toBe(true);
    expect(matcher("dir/aab(c).txt")).toBe(false);
  });

  it("expands every alternative group in a pattern", () => {
    const matcher = testFileMatcher("{unit,e2e}_*.{ts,js}");
    expect(matcher("unit_login.js")).toBe(true);
    expect(matcher("e2e_login.ts")).toBe(true);
    expect(matcher("smoke_login.ts")).toBe(false);
  });
});

describe("generated file classification", () => {
  it.each([
    "web/pnpm-lock.yaml",
    "package-lock.json",
    "uv.lock",
    "dist/app.min.js",
    "src/api/generated/client.ts",
    "generated/schema.ts",
    "Sources/Model.generated.swift",
  ])("recognizes %s", (path) => {
    expect(isGeneratedPath(path)).toBe(true);
  });

  it.each(["src/generator.ts", "web/package.json", "docs/locking.md"])(
    "leaves %s alone",
    (path) => {
      expect(isGeneratedPath(path)).toBe(false);
    },
  );
});
