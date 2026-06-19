/**
 * Cursor SDK model picker options for Polly (the `cursor` brain harness).
 *
 * Every entry's `id` is a Cursor SDK model id (e.g. `composer-2.5`), NOT a
 * display label — the in-chat switcher sends `id`, and the Cursor SDK rejects
 * a friendly label like `Composer` with `invalid_argument` (#547). The `label`
 * is the human-facing name shown in the dropdown.
 *
 * Lives in a leaf module (no React / store imports) so both the picker UI
 * (`ChatPage`) and tests can read it without a circular import. Mirrors
 * `claudeNativeModels.ts`.
 *
 * Sourced from the Cursor SDK's own available-models list. This is a static
 * snapshot; the robust long-term source is the runner-backed
 * `Cursor.models.list()` (a larger change tracked as a follow-up). The backend
 * `cursor_executor._resolve_model` chokepoint independently normalizes/guards
 * the value, so a model newer than this list still works.
 */
export const CURSOR_SDK_MODELS = [
  // Common picks first.
  { id: "auto", label: "Auto" },
  { id: "composer-2.5", label: "Composer" },
  { id: "default", label: "Default" },
  // Anthropic.
  { id: "claude-opus-4-8", label: "claude-opus-4-8" },
  { id: "claude-opus-4-7", label: "claude-opus-4-7" },
  { id: "claude-opus-4-6", label: "claude-opus-4-6" },
  { id: "claude-opus-4-5", label: "claude-opus-4-5" },
  { id: "claude-sonnet-4-6", label: "claude-sonnet-4-6" },
  { id: "claude-sonnet-4-5", label: "claude-sonnet-4-5" },
  { id: "claude-sonnet-4", label: "claude-sonnet-4" },
  { id: "claude-haiku-4-5", label: "claude-haiku-4-5" },
  // OpenAI.
  { id: "gpt-5.5", label: "gpt-5.5" },
  { id: "gpt-5.4", label: "gpt-5.4" },
  { id: "gpt-5.4-mini", label: "gpt-5.4-mini" },
  { id: "gpt-5.4-nano", label: "gpt-5.4-nano" },
  { id: "gpt-5.3-codex", label: "gpt-5.3-codex" },
  { id: "gpt-5.2", label: "gpt-5.2" },
  { id: "gpt-5.2-codex", label: "gpt-5.2-codex" },
  { id: "gpt-5.1", label: "gpt-5.1" },
  { id: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
  { id: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
  { id: "gpt-5-mini", label: "gpt-5-mini" },
  // Google.
  { id: "gemini-3.1-pro", label: "gemini-3.1-pro" },
  { id: "gemini-3.5-flash", label: "gemini-3.5-flash" },
  { id: "gemini-3-flash", label: "gemini-3-flash" },
  { id: "gemini-2.5-flash", label: "gemini-2.5-flash" },
] as const;
