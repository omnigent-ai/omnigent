/**
 * Claude-native model picker options: Claude Code's version-agnostic
 * aliases (not pinned IDs), so `/model opus` resolves to the latest
 * installed Opus — the list never drifts when a version retires, and the
 * ucode `ANTHROPIC_DEFAULT_*_MODEL` env pins redirect the same alias.
 *
 * Lives in a leaf module (no React / store imports) so both the picker UI
 * (`ChatPage`) and the store (`chatStore`) can read it without a circular
 * import.
 */
export const CLAUDE_NATIVE_MODELS = [
  // Ordered by capability tier, most powerful first.
  { id: "fable", label: "Fable" },
  { id: "opus", label: "Opus" },
  // Version-agnostic on purpose: which Sonnet the alias lands on is the
  // harness's call, and the live catalog's display name supersedes this
  // label wherever one has arrived. A pinned version here paints a wrong
  // claim during the pre-catalog window (it read "Sonnet 4.6" while the
  // catalog resolves the alias to Sonnet 5).
  { id: "sonnet", label: "Sonnet" },
  // Newer Sonnet, offered as an explicit opt-in via Claude Code's one
  // custom /model slot (ANTHROPIC_CUSTOM_MODEL_OPTION) — not a family
  // alias, and it does NOT change the default "sonnet" binding above.
  { id: "sonnet_5", label: "Sonnet 5" },
  { id: "haiku", label: "Haiku" },
] as const;
