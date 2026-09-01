/**
 * Pure ``/``-slash-token utilities shared by the in-session composer
 * (``ChatPage``) and the new-session launcher (``NewChatDialog``). The sibling
 * of ``composerMentions`` for the "/" trigger: kept free of React/state so both
 * surfaces decide *where* a command token starts identically — and so the
 * trigger logic is unit-testable without rendering.
 *
 * Ranking and filtering of the matches live in ``components/SlashCommandMenu``
 * (which the menu itself needs); only token detection and splicing are here.
 */

/** An active ``/`` token being typed in a composer. */
export interface SlashTokenState {
  /** Text typed after the ``/`` (no whitespace, no further ``/``). */
  query: string;
  /** Index of the ``/`` character in the textarea value. */
  start: number;
  /** Caret index (one past the last query char) — end of the token. */
  end: number;
  /**
   * True when this ``/`` opens the whole draft (nothing but whitespace
   * before it). Only a leading token can *invoke* something: the composer
   * routes a leading ``/cmd`` to a built-in or a ``slash_command`` event,
   * whereas a token typed mid-sentence is plain text the agent reads. Callers
   * use it to decide whether built-ins join the menu and whether selecting a
   * row may execute.
   */
  leading: boolean;
}

// Token preceding the caret: a "/" at the start of the inspected string or
// after whitespace, followed by a run with no whitespace and no further "/".
// (``^`` anchors to the start of the sliced ``before`` text, not a line — a
// mid-string "/" still matches because the newline before it counts as the
// ``\s``.) Excluding "/" from the run is what makes paths self-closing:
// "/etc/hosts" stops matching the moment the second slash is typed, and
// "and/or" never matches since its "/" follows a letter.
const SLASH_TOKEN_RE = /(?:^|\s)\/([^\s/]*)$/;

/**
 * Detect an in-progress ``/`` token immediately before the caret.
 *
 * Looks only at ``text`` up to ``caret`` so a trailing space (token finished)
 * closes the menu. Returns ``null`` when there is no active token.
 *
 * :param text: The full textarea value.
 * :param caret: The caret offset (``selectionStart``).
 * :returns: The active :class:`SlashTokenState`, or ``null``.
 */
export function detectSlashTokenAt(text: string, caret: number): SlashTokenState | null {
  const before = text.slice(0, caret);
  const m = SLASH_TOKEN_RE.exec(before);
  if (!m) return null;
  const query = m[1];
  // ``m.index`` points at the matched whitespace (or -1+1=0 at line start);
  // the "/" sits just before the captured query.
  const start = caret - query.length - 1;
  return { query, start, end: caret, leading: text.slice(0, start).trim() === "" };
}

/**
 * Replace an active token with the selected command name plus a trailing
 * space, leaving the rest of the draft untouched.
 *
 * The trailing space both closes the menu (a finished token no longer matches)
 * and puts the caret where an argument would go.
 *
 * :param text: The full textarea value.
 * :param token: The token being completed.
 * :param name: Slash-prefixed command name, e.g. ``"/deslop"``.
 * :returns: The rewritten text and the caret offset to restore.
 */
export function spliceSlashToken(
  text: string,
  token: SlashTokenState,
  name: string,
): { text: string; caret: number } {
  const inserted = `${name} `;
  return {
    text: text.slice(0, token.start) + inserted + text.slice(token.end),
    caret: token.start + inserted.length,
  };
}
