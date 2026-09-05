// Native-harness permission / approval / execution mode tables, shared by the
// new-session dialog and the fork dialog so the option lists, CLI-flag
// mappings, and default values can't drift between the two surfaces.
//
// Each entry's `args` are the `terminal_launch_args` the runner passes to the
// native CLI; a mode whose `args` are empty sends no flags (the CLI keeps its
// own configured default). Keep in sync with each CLI's `--help`.

import { CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE } from "@/lib/claudePermissionMode";

/** One selectable mode: value + label + blurb + the CLI flags it maps to. */
export interface NativeHarnessMode {
  value: string;
  label: string;
  description: string;
  args: string[];
}

// Antigravity (agy) permission control. agy exposes exactly ONE pre-emptive
// knob — `--dangerously-skip-permissions`, an all-or-nothing bypass — with no
// per-tool equivalent of acceptEdits/plan, so this is a two-value toggle rather
// than Claude's graded selector. "default" sends no flags and leaves agy's own
// request-review prompt in place. Keep in sync with `agy --help`.
export const AGY_NATIVE_DEFAULT_SKIP_MODE = "default";
export const AGY_NATIVE_SKIP_VALUE = "skip";
export const AGY_NATIVE_SKIP_MODES: NativeHarnessMode[] = [
  {
    value: AGY_NATIVE_DEFAULT_SKIP_MODE,
    label: "Ask every time",
    description: "Prompts before each tool runs",
    args: [],
  },
  {
    value: AGY_NATIVE_SKIP_VALUE,
    label: "Skip permissions",
    description: "Runs everything; no prompts or safety checks",
    args: ["--dangerously-skip-permissions"],
  },
];

// The Auto Harness's Permissions vocabulary: Default only. No cross-harness
// permission mapping exists, so the row stays locked and the create call sends
// no override — each CLI keeps the machine's own configuration.
export const AUTO_PERMISSION_MODE = {
  value: CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  label: "Default",
  description: "The picked harness keeps its own configured permissions",
} as const;
export const AUTO_PERMISSION_MODE_OPTIONS = [AUTO_PERMISSION_MODE] as const;

// Cursor execution modes. "default" sends no flags; other values map to CLI
// args passed via terminal_launch_args. Keep in sync with `cursor-agent --help`.
export const CURSOR_NATIVE_DEFAULT_EXEC_MODE = "default";
export const CURSOR_NATIVE_EXEC_MODES: NativeHarnessMode[] = [
  {
    value: "default",
    label: "Default",
    description: "Normal agent mode; prompts before running commands",
    args: [],
  },
  {
    value: "auto-review",
    label: "Auto-review",
    description: "Smart Auto: auto-runs safe tool calls and prompts for the rest",
    args: ["--auto-review"],
  },
  {
    value: "plan",
    label: "Plan",
    description: "Read-only planning; analyzes and proposes plans, no edits",
    args: ["--mode", "plan"],
  },
  {
    value: "ask",
    label: "Ask",
    description: "Q&A style; explains and answers questions (read-only)",
    args: ["--mode", "ask"],
  },
  {
    value: "yolo",
    label: "Yolo",
    description: "Runs everything without prompts or safety checks",
    args: ["--yolo"],
  },
];

// Codex approval presets matching the `/permissions` TUI popup.
// Each preset bundles a sandbox profile + approval policy, mirroring
// codex-rs/utils/approval-presets/src/lib.rs. "default" is the auto
// preset (workspace-write + on-request) and sends no flags so the
// runner uses Codex's built-in default.
// Keep in sync with `codex --help` and
// https://developers.openai.com/codex/agent-approvals-security
export const CODEX_NATIVE_DEFAULT_APPROVAL_MODE = "default";
export const CODEX_NATIVE_APPROVAL_MODES: NativeHarnessMode[] = [
  {
    value: "default",
    label: "Default",
    description: "Read/edit/run in workspace; approval for external edits or network",
    args: [],
  },
  {
    value: "full-access",
    label: "Full access",
    description: "Edit any file and access the internet without approval",
    args: ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
  },
  {
    value: "read-only",
    label: "Read only",
    description: "Read files only; approval required for edits, commands, or network",
    args: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
  },
];

// Conversation-label key for the DANGEROUS codex full-bypass opt-in. When
// set to "1" the runner launches Codex with
// `--dangerously-bypass-approvals-and-sandbox` (no approval prompts, no
// command sandbox) — see omnigent.stores.conversation_store
// CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY. Stored as a label (cheap thread
// metadata) so it survives reload. Mutually exclusive in spirit with the
// approval-mode presets above: when bypass is on the runner strips any
// `--sandbox` / `--ask-for-approval` flags those presets would emit.
export const CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY = "omnigent.codex_native.bypass_sandbox";
// Bypass is the most-permissive Codex approval stance — presented as a 4th
// option in the Codex approval dropdown (Codex only; OpenCode shares the
// presets above but has no bypass). It rides as a conversation label, not
// terminal_launch_args, so its `args` are empty and it's handled specially.
export const CODEX_NATIVE_BYPASS_APPROVAL_VALUE = "bypass";
export const CODEX_NATIVE_BYPASS_APPROVAL_OPTION: NativeHarnessMode = {
  value: CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  label: "Bypass approvals & sandbox",
  description: "Runs Codex with no approval prompts and no command sandbox",
  args: [],
};
