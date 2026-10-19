import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  codeRunComposerText,
  codeRunLanguage,
  confirmCodeRuns,
  deriveCodeRunCommand,
  harnessReceivesCodeRuns,
  isCodeRunConfirmed,
  normalizeBlockText,
  sha256Hex,
  startCodeRun,
  stopCodeRun,
  type CodeRunLanguage,
} from "./codeRuns";

const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as unknown as Response;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("sha256Hex", () => {
  it.each(["", "abc", "echo hi\nls -la", "naïve ✓ 漢字 🚀", "x".repeat(1000)])(
    "matches Node's SHA-256 for %j",
    (text) => {
      expect(sha256Hex(text)).toBe(createHash("sha256").update(text, "utf8").digest("hex"));
    },
  );
});

describe("normalizeBlockText", () => {
  it("turns CRLF and CR into LF and drops trailing newlines, like the server", () => {
    expect(normalizeBlockText("a\r\nb\rc\n\n")).toBe("a\nb\nc");
  });
});

describe("codeRunLanguage", () => {
  it.each([
    ["bash", "ls", "shell"],
    ["sh", "ls", "shell"],
    ["shell", "ls", "shell"],
    ["zsh", "ls", "shell"],
    ["console", "$ ls\nout", "shell"],
    ["Python", "print(1)", "python"],
    ["javascript", "console.log(1)", "node"],
    ["node", "console.log(1)", "node"],
    ["ts", "let x = 1", null],
    ["text", "$ ls", null],
    ["", "$ ls\n\n$ pwd", "shell"],
    ["", "$ ls\nfile", null],
    ["", "", null],
  ])("tag %j with %j runs as %j", (tag, code, expected) => {
    expect(codeRunLanguage(tag, code)).toBe(expected);
  });
});

describe("deriveCodeRunCommand", () => {
  it("strips shell prompts, keeps only prompt lines for console, passes source through", () => {
    expect(deriveCodeRunCommand("shell", "$ ls\n% pwd\necho hi", false)).toBe("ls\npwd\necho hi");
    expect(deriveCodeRunCommand("shell", "$ ls\nfile.txt\n$ pwd", true)).toBe("ls\npwd");
    expect(deriveCodeRunCommand("python", "$ not a prompt", false)).toBe("$ not a prompt");
  });
});

// Shared with tests/server/routes/test_code_runs_routes.py, which checks the
// server's cleaning against the same cases.
const DERIVATION_CASES = (
  JSON.parse(
    readFileSync(
      path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        "../../../tests/fixtures/code_runs/command_derivation.json",
      ),
      "utf8",
    ),
  ) as {
    cases: {
      name: string;
      language: CodeRunLanguage;
      console: boolean;
      code: string;
      command: string;
    }[];
  }
).cases;

describe("deriveCodeRunCommand matches the server's cleaning", () => {
  it.each(DERIVATION_CASES.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    expect(deriveCodeRunCommand(c.language, normalizeBlockText(c.code), c.console)).toBe(c.command);
  });
});

// Shared with tests/server/routes/test_code_runs_routes.py: whether the server
// runs an untagged block as shell.
const ELIGIBILITY_CASES = (
  JSON.parse(
    readFileSync(
      path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        "../../../tests/fixtures/code_runs/untagged_eligibility.json",
      ),
      "utf8",
    ),
  ) as { cases: { name: string; code: string; runnable: boolean }[] }
).cases;

describe("codeRunLanguage matches the server's untagged check", () => {
  it.each(ELIGIBILITY_CASES.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    expect(codeRunLanguage("", normalizeBlockText(c.code))).toBe(c.runnable ? "shell" : null);
  });
});

describe("harnessReceivesCodeRuns", () => {
  it.each([
    ["openai-agents", true],
    ["claude-sdk", true],
    [null, true],
    ["claude-native", true],
    ["codex-native", true],
    ["cursor-native", false],
    ["native-pi", false],
    ["goose-native", false],
  ])("%j receives runs: %j", (harness, expected) => {
    expect(harnessReceivesCodeRuns(harness)).toBe(expected);
  });
});

describe("code run confirmation", () => {
  it("is remembered per session in browser storage", () => {
    expect(isCodeRunConfirmed("conv_a")).toBe(false);
    confirmCodeRuns("conv_a");
    expect(isCodeRunConfirmed("conv_a")).toBe(true);
    expect(isCodeRunConfirmed("conv_b")).toBe(false);
  });
});

describe("codeRunComposerText", () => {
  it("fences the command, exit status, and output", () => {
    const text = codeRunComposerText({
      language: "shell",
      command: "echo hi",
      status: "exited",
      exitCode: 0,
      output: [
        { stream: "stdout", text: "hi\n" },
        { stream: "stderr", text: "warn\n" },
      ],
    });
    expect(text).toBe(
      "```text\nCommand (shell):\necho hi\n\nOutput:\nhi\nwarn\n\nExited with code 0\n```",
    );
  });

  it("keeps the end of long output and outgrows backtick runs in the content", () => {
    const text = codeRunComposerText({
      language: "python",
      command: "print('```')",
      status: "timed_out",
      exitCode: null,
      output: [{ stream: "stdout", text: "a".repeat(5000) + "END" }],
    });
    expect(text.startsWith("````text\n")).toBe(true);
    expect(text.endsWith("\nTimed out\n````")).toBe(true);
    expect(text).toContain("(output trimmed)\n");
    expect(text).toContain("aEND");
    expect(text.length).toBeLessThan(4300);
  });
});

describe("startCodeRun", () => {
  it("posts the block and returns the run id", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ run_id: "run_1" }, 202));
    const result = await startCodeRun("conv_a", {
      messageId: "msg_1",
      blockHash: "abc",
      language: "shell",
      code: "ls",
      approved: true,
    });
    expect(result).toEqual({ ok: true, runId: "run_1" });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toContain("/v1/sessions/conv_a/code-runs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      message_id: "msg_1",
      block_hash: "abc",
      language: "shell",
      code: "ls",
      approved: true,
    });
  });

  it("returns a policy refusal as a value", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ code: "needs_approval", reason: "Risky" }, 409));
    await expect(
      startCodeRun("conv_a", { messageId: "m", blockHash: "h", language: "shell", code: "ls" }),
    ).resolves.toEqual({ ok: false, code: "needs_approval", reason: "Risky" });
  });

  it("reads a validation error envelope as a refusal", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "invalid_input", message: "code is not a block" } }, 400),
    );
    await expect(
      startCodeRun("conv_a", { messageId: "m", blockHash: "h", language: "shell", code: "ls" }),
    ).resolves.toEqual({ ok: false, code: "invalid_input", reason: "code is not a block" });
  });
});

describe("stopCodeRun", () => {
  it("posts to the run's stop route", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ stopped: true }));
    await stopCodeRun("conv_a", "run_1");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toContain("/v1/sessions/conv_a/code-runs/run_1/stop");
    expect(init.method).toBe("POST");
  });
});
