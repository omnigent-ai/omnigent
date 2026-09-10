// Auto-opening the dbcert SSO login an agent's terminal asks for.
//
// Databricks' ``isaac`` wrapper refreshes the short-lived dbcert credential
// every agent needs before handing off to Claude/Codex. When that credential is
// stale, its launcher runs ``dbcert``, which starts a loopback callback
// listener, prints
//
//   If the browser does not open automatically, please open the following URL:
//
//       https://databricks.okta.com/oauth2/v1/authorize?...
//
// and tries to open that URL itself. Its opener is a local one (``open`` /
// ``xdg-open``), so when the agent runs on a remote dev box there is no browser
// to reach: the launch sits blocked on a login whose link is only visible as
// text in the terminal pane. Since the pane's bytes already stream to this UI,
// we scan them and open the link in the browser the user is actually looking at
// — the same tab they would get by clicking it by hand.
//
// Detection is deliberately narrow. A match needs BOTH an allowlisted Okta
// authorization endpoint AND a ``redirect_uri`` pointing at dbcert's own
// ``/v1/dbcert/callback`` handler, so neither an unrelated Okta link an agent
// happens to print nor a different OAuth flow can trigger an automatic tab.

/**
 * Okta authorization hosts dbcert is configured against, across its four
 * environments (prod, prod-emergency, prod-aws-gov, dev). An authorize URL on
 * any other host is not dbcert's, whatever its query string says.
 */
const DBCERT_OKTA_HOSTS = new Set([
  "databricks.okta.com",
  "regulated-databricks.okta-gov.com",
  "corpengdatabricks.oktapreview.com",
]);

const OKTA_AUTHORIZE_PATH = "/oauth2/v1/authorize";

/**
 * dbcert's OAuth callback, served by the short-lived loopback listener it runs
 * for the duration of a login. Its port varies by environment, so the match is
 * on host and path only.
 */
const DBCERT_CALLBACK_PATH = "/v1/dbcert/callback";
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1"]);

/**
 * Candidate authorize URLs, with a required terminator after the match so a URL
 * cut in half by a chunk boundary is never committed as if it were whole: a
 * truncated authorize URL still parses and can still carry a valid
 * ``redirect_uri``, and opening it would land the user on an Okta error. dbcert
 * prints the URL followed by newlines, so a real one always brings its own
 * terminator.
 */
const AUTHORIZE_URL = new RegExp(
  String.raw`https?://[^\s'"\`<>)\]}]+\/oauth2\/v1\/authorize\?[^\s'"\`<>)\]}]+(?=[\s'"\`<>)\]}])`,
  "gi",
);

/**
 * How much recent output to keep for the scan. An Okta authorize URL carries
 * PKCE, nonce, state and scopes, so it runs several hundred characters; the tail
 * has to be able to hold a whole one plus whatever precedes it in the frame that
 * split it.
 */
const TAIL_CHARS = 2048;

/**
 * ESC and BEL, built rather than written as escapes so the patterns below are
 * assembled from a non-literal string: a control character inside a regex
 * literal is a lint error, and the alternative would be suppressing it.
 */
const ESC = String.fromCharCode(0x1b);
const BEL = String.fromCharCode(0x07);

/** OSC (``ESC ]``) through either terminator: BEL or ST (``ESC \``). */
const OSC_SEQUENCE = new RegExp(`${ESC}\\][^${BEL}${ESC}]*(?:${BEL}|${ESC}\\\\)`, "g");
/** CSI colour/style and cursor sequences. */
const CSI_SEQUENCE = new RegExp(`${ESC}\\[[0-9;]*[a-zA-Z]`, "g");
/** CSI private-mode sequences (``ESC [ ?``). */
const CSI_PRIVATE_SEQUENCE = new RegExp(`${ESC}\\[\\?[0-9;]*[a-zA-Z]`, "g");

/**
 * Strip ANSI escapes before scanning.
 *
 * dbcert wraps the URL in an OSC 8 hyperlink whenever its stderr is a terminal —
 * which it always is inside a pane — so the raw stream carries the URL twice:
 * once inside ``ESC ] 8 ; ; <uri> ST`` and once as the visible text. Removing
 * the OSC wrapper leaves the visible copy intact and stops the escape's ``ESC \``
 * terminator being read as part of a URL. Both OSC terminators (BEL and ST) are
 * handled.
 */
function stripAnsi(text: string): string {
  return text.replace(OSC_SEQUENCE, "").replace(CSI_SEQUENCE, "").replace(CSI_PRIVATE_SEQUENCE, "");
}

/** Whether ``raw`` is dbcert's own loopback callback URL. */
function isDbcertCallback(raw: string): boolean {
  try {
    const url = new URL(raw);
    return LOOPBACK_HOSTS.has(url.hostname) && url.pathname === DBCERT_CALLBACK_PATH;
  } catch {
    return false;
  }
}

/**
 * Whether ``candidate`` is an Okta authorize URL for a dbcert login.
 *
 * :param candidate: A URL found in terminal output.
 */
export function isDbcertLoginUrl(candidate: string): boolean {
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return false;
  }
  if (url.protocol !== "https:") return false;
  if (!DBCERT_OKTA_HOSTS.has(url.hostname)) return false;
  if (url.pathname !== OKTA_AUTHORIZE_PATH) return false;
  const redirect = url.searchParams.get("redirect_uri");
  return !!redirect && isDbcertCallback(redirect);
}

/**
 * The dbcert login URL in a chunk of terminal output, or ``null``.
 *
 * Returns the LAST match: dbcert prints one URL per login, but a pane redraw can
 * replay an older one, and the most recent belongs to the live listener.
 *
 * :param text: Raw (escape-bearing) terminal output.
 */
export function extractDbcertLoginUrl(text: string): string | null {
  // Cheap gate before the scan: every dbcert authorize URL carries its callback
  // path inside the percent-encoded `redirect_uri`, so the literal "dbcert" is
  // always present. Ordinary agent output is not.
  if (!text.includes("dbcert")) return null;
  const clean = stripAnsi(text);
  AUTHORIZE_URL.lastIndex = 0;
  let found: string | null = null;
  let match: RegExpExecArray | null;
  while ((match = AUTHORIZE_URL.exec(clean)) !== null) {
    if (isDbcertLoginUrl(match[0])) found = match[0];
  }
  return found;
}

/**
 * Open a dbcert login URL in a new tab.
 *
 * Returns whether the browser accepted it. There is no user gesture behind
 * terminal output, so a popup blocker can refuse the tab and hand back null —
 * callers must surface a refusal as something clickable rather than dropping it.
 * In the desktop app this goes through the shell's external-link handling, which
 * has no such restriction.
 *
 * :param url: The login URL to open.
 * :param win: Window to open through; injectable for tests.
 */
export function openDbcertLogin(url: string, win: Window = window): boolean {
  return win.open(url, "_blank", "noopener,noreferrer") !== null;
}

/**
 * Build a watcher that reports each distinct dbcert login URL once.
 *
 * The returned function is fed raw terminal output as it arrives; it keeps a
 * bounded tail so a URL split across frames is still found, and calls
 * ``onLogin`` the first time each URL is seen.
 *
 * :param onLogin: Called with a newly seen login URL.
 */
export function createDbcertLoginWatcher(onLogin: (url: string) => void): (chunk: string) => void {
  let tail = "";
  let lastSeen: string | null = null;
  return (chunk: string) => {
    if (!chunk) return;
    tail = (tail + chunk).slice(-TAIL_CHARS);
    const url = extractDbcertLoginUrl(tail);
    if (!url || url === lastSeen) return;
    lastSeen = url;
    onLogin(url);
  };
}
