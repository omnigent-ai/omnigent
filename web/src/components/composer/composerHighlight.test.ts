import { createElement, useRef } from "react";
import { fireEvent, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { supportsNativeShellInput } from "@/lib/harnessSetup";
import {
  SHELL_MENTION_REFUSAL,
  composerHighlightSplit,
  splitKnownSlashCommand,
  splitNativeShellCommand,
  useComposerHighlight,
  usePublishComposerCommands,
  usePublishedComposerCommands,
} from "./composerHighlight";

const KNOWN = ["/help", "/team", "$review"];

describe("composerHighlightSplit commands", () => {
  it.each([
    ["/team fix it", { before: "", token: "/team", after: " fix it" }],
    ["  /team", { before: "  ", token: "/team", after: "" }],
    ["$review the diff", { before: "", token: "$review", after: " the diff" }],
  ])("tints the known command in %j", (value, split) => {
    expect(composerHighlightSplit(value, KNOWN, null)).toEqual({ kind: "command", ...split });
  });

  it.each([
    ["an unknown command", "/cross-review fix it"],
    ["a command after prose", "x /team"],
    ["a different case", "/Team fix it"],
    ["a longer word", "/teams fix it"],
    ["a path", "/etc/hosts"],
  ])("leaves %s untinted", (_label, value) => {
    expect(composerHighlightSplit(value, KNOWN, "codex-native")).toBeNull();
  });

  it("tints an any-case command as typed, but a skill only in its exact case", () => {
    expect(
      composerHighlightSplit("/Goal Fix CI", ["/goal", "/team"], null, undefined, ["/goal"]),
    ).toEqual({
      kind: "command",
      before: "",
      token: "/Goal",
      after: " Fix CI",
    });
    expect(
      composerHighlightSplit("/Team fix it", ["/goal", "/team"], null, undefined, ["/goal"]),
    ).toBeNull();
  });
});

describe("splitKnownSlashCommand", () => {
  it("keeps a plugin-namespaced skill name whole", () => {
    expect(
      splitKnownSlashCommand("/dev-productivity:simplify now", ["/dev-productivity:simplify"]),
    ).toEqual({ before: "", token: "/dev-productivity:simplify", after: " now" });
  });

  it("treats a known skill name with spaces as one token", () => {
    const name = "/Simplified Technical English (ASD-STE100)";
    expect(splitKnownSlashCommand(`${name} rewrite this`, ["/compact", name])).toEqual({
      before: "",
      token: name,
      after: " rewrite this",
    });
    // Without a catalog match nothing is tinted, not even the first word.
    expect(splitKnownSlashCommand(`${name} rewrite this`, ["/compact"])).toBeNull();
  });

  it("matches a known skill whose first word is not command-shaped", () => {
    const name = "/Node.js Best Practices";
    expect(splitKnownSlashCommand(`${name} here`, [name])).toEqual({
      before: "",
      token: name,
      after: " here",
    });
    expect(splitKnownSlashCommand(`${name} here`, [])).toBeNull();
  });
});

describe("composerHighlightSplit native shell", () => {
  it.each([
    ["codex-native", "!ls -la", { before: "", token: "!", after: "ls -la" }],
    ["codex-native", " !ls", { before: " ", token: "!", after: "ls" }],
    ["codex-native", "! git status", { before: "", token: "!", after: " git status" }],
    // Python's strip() counts \x1c as whitespace but not \ufeff.
    ["codex-native", "\x1c!echo ok", { before: "\x1c", token: "!", after: "echo ok" }],
    ["codex-native", "!\ufeff", { before: "", token: "!", after: "\ufeff" }],
    ["claude-native", "!ls -la", { before: "", token: "!", after: "ls -la" }],
    // The TUI enters bash mode on the first `!`; the second is the command.
    ["claude-native", "!!ls", { before: "", token: "!", after: "!ls" }],
    ["claude-native", "!\techo", { before: "", token: "!", after: "\techo" }],
    // The paste encoder drops the \x05, so the TUI sees a leading `!`.
    ["claude-native", "\x05!echo ok", { before: "\x05", token: "!", after: "echo ok" }],
  ])("%s tints the `!` of %j", (harness, value, split) => {
    expect(composerHighlightSplit(value, KNOWN, harness)).toEqual({ kind: "shell", ...split });
  });

  it.each([
    ["codex-native", "an escaped `!!`", "!!ls"],
    ["claude-native", "a `!` after whitespace", " !ls"],
    ["codex-native", "a bare `!`", "!"],
    ["claude-native", "a bare `!`", "!"],
    ["codex-native", "a blank command", "!   \n"],
    ["claude-native", "a blank command", "!   \n"],
    ["codex-native", "a mid-text `!`", "run !ls"],
    ["codex-native", "a `!` after a BOM", "\ufeff!echo ok"],
    ["codex-native", "a whitespace-only command", "!\x1c"],
    ["claude-native", "a command the encoder drops", "!\x1c"],
    ["claude-native", "a `!` after a kept DEL", "\x7f!x"],
    ["claude-native", "a `!` after a kept line break", "\r!x"],
    ["claude-native", "a `!` after a kept tab", "\t!x"],
    ["claude-native", "a `!` after a kept C1 control", "\x85!x"],
  ])("%s keeps %s as model input", (harness, _label, value) => {
    expect(splitNativeShellCommand(value, harness)).toBeNull();
  });

  it("never enters shell mode when the harness does not run `!`", () => {
    expect(composerHighlightSplit("!ls", KNOWN, null)).toBeNull();
  });
});

describe("useComposerHighlight shell hint", () => {
  function Hint({ value }: { value: string }) {
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const { hint } = useComposerHighlight({
      value,
      commandNames: KNOWN,
      enabled: true,
      nativeShell: "codex-native",
      mentionTokens: [],
      textareaRef,
    });
    return hint;
  }

  it("announces shell mode through a live region that is mounted before it", () => {
    const view = render(createElement(Hint, { value: "ls" }));
    const status = screen.getByTestId("composer-shell-mode-status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("");
    view.rerender(createElement(Hint, { value: "!ls" }));
    // Same node, so assistive tech sees a text change rather than a new region.
    expect(screen.getByTestId("composer-shell-mode-status")).toBe(status);
    expect(status).toHaveTextContent("Shell command");
    // The visible hint stays out of reading order, so the mode is announced once.
    expect(screen.getByTestId("composer-shell-mode-indicator")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
});

describe("useComposerHighlight CodeMirror token", () => {
  const tokenFor = (value: string) =>
    renderHook(() =>
      useComposerHighlight({
        value,
        commandNames: KNOWN,
        enabled: true,
        nativeShell: "codex-native",
        mentionTokens: [],
        textareaRef: { current: null },
      }),
    ).result.current.input.commandToken;

  it("hands a command over as text, so the editor still checks it leads a whole word", () => {
    expect(tokenFor("  /team fix")).toBe("/team");
  });

  it("places the shell `!` at its offset", () => {
    expect(tokenFor("  !ls")).toEqual({ token: "!", start: 2 });
  });

  it("hands over nothing for prose", () => {
    expect(tokenFor("fix it")).toBeNull();
  });
});

describe("useComposerHighlight refusal", () => {
  function Hint({ mentions }: { mentions: unknown[] }) {
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const { hint, refusal } = useComposerHighlight({
      value: "!cat",
      commandNames: KNOWN,
      enabled: true,
      nativeShell: "codex-native",
      mentionTokens: [],
      textareaRef,
      sendContext: { mentions },
    });
    return createElement("div", { "data-refusal": refusal ?? "" }, hint);
  }

  it("shows why a `!` send that carries a tag can't go out", () => {
    const view = render(createElement(Hint, { mentions: [{}] }));
    expect(screen.getByTestId("composer-shell-mode-indicator")).toHaveTextContent(
      SHELL_MENTION_REFUSAL,
    );
    expect(screen.getByTestId("composer-shell-mode-status")).toHaveTextContent(
      SHELL_MENTION_REFUSAL,
    );
    expect(view.container.firstElementChild).toHaveAttribute("data-refusal", SHELL_MENTION_REFUSAL);

    view.rerender(createElement(Hint, { mentions: [] }));
    expect(screen.getByTestId("composer-shell-mode-indicator")).toHaveTextContent("Shell command");
    expect(view.container.firstElementChild).toHaveAttribute("data-refusal", "");
  });
});

describe("useComposerHighlight mirror", () => {
  function Mirror({
    value,
    mentionTokens,
    enabled = true,
  }: {
    value: string;
    mentionTokens: string[];
    enabled?: boolean;
  }) {
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const { backdrop, input } = useComposerHighlight({
      value,
      commandNames: KNOWN,
      enabled,
      nativeShell: "codex-native",
      mentionTokens,
      textareaRef,
    });
    return createElement(
      "div",
      null,
      createElement("textarea", { ref: textareaRef, value, readOnly: true, ...input }),
      backdrop,
    );
  }

  it.each([
    ["a command", "/team ask @app.ts", "/team"],
    ["a shell command", "!cat @app.ts", "!"],
  ])("paints %s and a mention pill in one mirror", (_label, value, token) => {
    render(createElement(Mirror, { value, mentionTokens: ["@app.ts"] }));
    const [overlay, ...extra] = screen.getAllByTestId("composer-highlight-overlay");
    expect(extra).toHaveLength(0);
    expect(overlay).toHaveTextContent(value);
    expect(overlay.querySelector(".text-brand-accent")?.textContent).toBe(token);
    expect(screen.getByTestId("composer-mention-token")).toHaveTextContent("@app.ts");
    expect(screen.getByRole("textbox")).toHaveClass("text-transparent");
  });

  it("keeps painting mention pills while the command tint is disabled", () => {
    render(
      createElement(Mirror, { value: "/team @app.ts", mentionTokens: ["@app.ts"], enabled: false }),
    );
    const overlay = screen.getByTestId("composer-highlight-overlay");
    expect(overlay).toHaveTextContent("/team @app.ts");
    // The pill is the only tinted run; the command token is not split out.
    expect(overlay.querySelector(".text-brand-accent:not([data-testid])")).toBeNull();
    expect(screen.getByTestId("composer-mention-token")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).not.toHaveAttribute("data-slash-command");
  });

  it("follows the textarea's scroll position", () => {
    const value = ["/team plan", ...Array.from({ length: 40 }, (_, i) => `line ${i}`)].join("\n");
    render(createElement(Mirror, { value, mentionTokens: [] }));
    const textarea = screen.getByRole("textbox");
    const overlay = screen.getByTestId("composer-highlight-overlay");
    // jsdom keeps no scroll offset, so back both with plain properties.
    for (const element of [textarea, overlay]) {
      let scrollTop = 0;
      Object.defineProperty(element, "scrollTop", {
        configurable: true,
        get: () => scrollTop,
        set: (next: number) => {
          scrollTop = next;
        },
      });
    }
    textarea.scrollTop = 240;
    fireEvent.scroll(textarea);
    expect(overlay.scrollTop).toBe(240);
  });

  it("mounts no mirror for prose without mentions", () => {
    render(createElement(Mirror, { value: "just prose", mentionTokens: ["@app.ts"] }));
    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
    expect(screen.getByRole("textbox")).not.toHaveClass("text-transparent");
  });
});

describe("usePublishComposerCommands", () => {
  it.each([
    { label: "the older one", unmountFirst: "older", survivor: ["/help"] },
    { label: "the newer one", unmountFirst: "newer", survivor: ["/team"] },
  ] as const)(
    "keeps the surviving composer's commands when $label of two publishers unmounts first",
    ({ unmountFirst, survivor }) => {
      const older = renderHook(() => usePublishComposerCommands("conv_dup", ["/team"], null));
      const newer = renderHook(() => usePublishComposerCommands("conv_dup", ["/help"], null));
      const read = renderHook(() => usePublishedComposerCommands("conv_dup"));
      expect(read.result.current?.commandNames).toEqual(["/help"]);

      const [first, second] = unmountFirst === "older" ? [older, newer] : [newer, older];
      first.unmount();
      expect(read.result.current?.commandNames).toEqual(survivor);
      second.unmount();
      expect(read.result.current).toBeUndefined();
    },
  );

  it("keeps the newer composer on top when the older one changes its catalog", () => {
    const older = renderHook(
      ({ names }: { names: string[] }) => usePublishComposerCommands("conv_order", names, null),
      { initialProps: { names: ["/team"] } },
    );
    const newer = renderHook(() => usePublishComposerCommands("conv_order", ["/help"], null));
    const read = renderHook(() => usePublishedComposerCommands("conv_order"));

    older.rerender({ names: ["/team", "/plan"] });
    expect(read.result.current?.commandNames).toEqual(["/help"]);
    newer.unmount();
    expect(read.result.current?.commandNames).toEqual(["/team", "/plan"]);
    older.unmount();
    expect(read.result.current).toBeUndefined();
  });

  it("leaves another conversation's commands alone when one updates or unmounts", () => {
    const a = renderHook(
      ({ names, shell }: { names: string[]; shell: string | null }) =>
        usePublishComposerCommands("conv_a", names, shell),
      { initialProps: { names: ["/team"], shell: "claude-native" as string | null } },
    );
    const b = renderHook(() => usePublishComposerCommands("conv_b", ["/help"], "codex-native"));
    const readA = renderHook(() => usePublishedComposerCommands("conv_a"));
    const readB = renderHook(() => usePublishedComposerCommands("conv_b"));
    const bBefore = readB.result.current;
    expect(bBefore).toEqual({ commandNames: ["/help"], nativeShell: "codex-native" });

    a.rerender({ names: ["/review"], shell: null });
    expect(readA.result.current).toEqual({ commandNames: ["/review"], nativeShell: null });
    expect(readB.result.current).toBe(bBefore);

    a.unmount();
    expect(readA.result.current).toBeUndefined();
    expect(readB.result.current).toBe(bBefore);

    b.unmount();
    expect(readB.result.current).toBeUndefined();
  });
});

describe("supportsNativeShellInput", () => {
  it.each([
    ["claude-native", true],
    ["codex-native", true],
    // Aliases are not what the server compares against.
    ["native-codex", false],
    ["native-claude", false],
    ["codex", false],
    ["claude-sdk", false],
    ["pi", false],
    [null, false],
  ])("%s -> %s", (harness, expected) => {
    expect(supportsNativeShellInput(harness)).toBe(expected);
  });
});
