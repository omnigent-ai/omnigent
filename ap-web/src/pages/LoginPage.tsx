/**
 * The sign-in page for the ``accounts`` auth provider.
 *
 * Reached when:
 *
 * - An unauthed user lands on any SPA route → ``resolveIdentity()``
 *   in ``identity.ts`` hits ``GET /v1/me``, the server returns 401
 *   with ``login_url: "/login"``, the browser navigates here.
 * - The user clicks "Sign out" anywhere in the chrome.
 * - The magic-redeem URL fails or expires — accounts_auth.py's
 *   ``/auth/magic/redeem`` handler 302s here with
 *   ``?magic=expired``.
 *
 * On successful login the cookie is set by the server's
 * ``POST /auth/login`` Set-Cookie header; we just navigate the
 * browser back to the ``return_to`` path (or ``/``) and let
 * ``resolveIdentity()`` re-run.
 *
 * Username defaults to whatever the last successful sign-in used on
 * this browser (cached in localStorage). On a fresh browser / private
 * window the field is empty — we don't hardcode "admin" because the
 * bootstrap admin's name now follows the host OS user
 * (``getpass.getuser()``), not a fixed string. Surfacing it via an
 * unauth endpoint would leak the admin's username to anyone who can
 * reach the deploy.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getMe, login as loginRequest } from "@/lib/accountsApi";
import { withBasePath } from "@/lib/basePath";

const DEFAULT_RETURN_TO = "/";
const LAST_USERNAME_KEY = "omnigent.lastLoginUsername";

function readLastUsername(): string {
  try {
    return window.localStorage.getItem(LAST_USERNAME_KEY) ?? "";
  } catch {
    // localStorage can throw in sandboxed iframes / blocked-cookies mode.
    return "";
  }
}

function rememberUsername(value: string): void {
  try {
    window.localStorage.setItem(LAST_USERNAME_KEY, value);
  } catch {
    // Best-effort — see readLastUsername.
  }
}

export function LoginPage() {
  const [params] = useSearchParams();
  // `return_to` is set by both identity.ts (on 401 redirect) and the
  // server-side magic-redeem 302 fallback. Trust only same-origin
  // paths — never a fully-qualified URL — to prevent open-redirect.
  const returnTo = sanitizeReturnTo(params.get("return_to"));
  const magicError = params.get("magic"); // "expired" | "missing" | null

  const [username, setUsername] = useState(readLastUsername);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(
    magicError === "expired"
      ? "That sign-in link has expired. Enter your password to sign in."
      : magicError === "missing"
        ? "That sign-in link is no longer valid. Enter your password to sign in."
        : null,
  );

  // Already signed in? Don't show a login form to an authenticated
  // user — bounce them to where they were headed (or home). Covers
  // someone hitting /login directly, a bookmarked /login, or a
  // back-button after auth. Hard-navigate so identity.ts re-runs.
  useEffect(() => {
    void (async () => {
      const account = await getMe();
      if (account !== null) {
        window.location.href = returnTo;
      }
    })();
    // returnTo is derived from the URL once; re-checking on change isn't
    // meaningful for a one-shot "am I already authed?" probe.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Focus the empty field on mount: username when no remembered value,
  // password when the username is pre-filled from localStorage.
  useEffect(() => {
    const targetId = username ? "login-password" : "login-username";
    const el = document.getElementById(targetId);
    if (el instanceof HTMLInputElement) {
      el.focus();
    }
    // Run only on mount — re-running on `username` changes would
    // steal focus while the user is typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);

    const result = await loginRequest({ username, password });
    if (result.ok) {
      rememberUsername(username);
      // Hard-navigate so identity.ts re-runs and every cached
      // query is rebuilt against the new session.
      window.location.href = returnTo;
      return;
    }
    setSubmitting(false);
    setError(result.error);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="text-sm text-muted-foreground">Welcome to Omnigent.</p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="login-username" className="text-sm font-medium leading-none">
              Username
            </label>
            <Input
              id="login-username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              required
            />
            <p className="text-xs text-muted-foreground">
              On a fresh install your username is your machine login (the output of{" "}
              <code className="font-mono">whoami</code>), unless an admin set a different one.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="login-password" className="text-sm font-medium leading-none">
              Password
            </label>
            <Input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
            />
          </div>

          {error !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {error}
            </div>
          )}

          <Button type="submit" className="w-full" disabled={submitting || password.length === 0}>
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="text-center text-xs text-muted-foreground">
          On a fresh install the initial admin password was printed to the server's stderr and saved
          to{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono">
            ~/.omnigent/admin-credentials
          </code>
          .
        </p>
      </div>
    </div>
  );
}

/**
 * Reject anything that isn't a relative path on the same origin.
 *
 * Defense against an open-redirect via crafted ``?return_to=`` —
 * an attacker who can get a victim to click a link to
 * ``/login?return_to=https://evil.com`` would otherwise have us
 * land them on the attacker's page after auth.
 *
 * Prefix checks alone are not enough: the value reaches us already
 * URL-decoded, so ``%2F%5Cevil.com`` becomes ``/\evil.com`` — which
 * passes a naive ``startsWith("/")`` + ``!startsWith("//")`` pair but
 * resolves to ``https://evil.com/`` because WHATWG URL parsing treats
 * backslashes as path separators for special schemes. So we require a
 * leading ``/`` (not ``//`` or ``/\``) and then resolve against the
 * current origin and confirm the result stays same-origin before
 * trusting it.
 */
function sanitizeReturnTo(raw: string | null): string {
  // The default lands the user at the app root under the active base path
  // (e.g. `/proxy/6767/` behind a subpath proxy), not the origin root.
  const fallback = withBasePath(DEFAULT_RETURN_TO);
  if (raw === null || raw === "") return fallback;
  // Must be an absolute path, not protocol-relative (`//host`) or a
  // backslash variant (`/\host`) the URL parser rewrites to one.
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) {
    return fallback;
  }
  try {
    const resolved = new URL(raw, window.location.origin);
    if (resolved.origin !== window.location.origin) return fallback;
    // Re-serialize so the sink gets the parser's normalized path, never
    // the raw backslash-laden input.
    return resolved.pathname + resolved.search + resolved.hash;
  } catch {
    // `new URL` throws on malformed input — treat anything unparseable
    // as untrusted and fall back to the safe default.
    return fallback;
  }
}
