/**
 * First-run "Create admin" page (shown when ``/v1/info`` reports
 * ``needs_setup``).
 *
 * On a fresh accounts deploy no admin exists yet. The first visitor
 * claims it here by choosing a username + password; the server's
 * ``POST /auth/setup`` creates the admin (hard-gated to the
 * zero-admin state), sets the session cookie, and we navigate to
 * ``/`` signed in. This is the remote-deploy path (Docker / Render /
 * Railway) where there's no terminal to read a password from — and
 * locally the server auto-opens the browser straight here.
 *
 * Once an admin exists the server 409s ``/auth/setup`` and
 * ``needs_setup`` flips false, so App routes to LoginPage instead and
 * this page is never reachable.
 *
 * Mounted outside the AppShell (like Login/Register) — the chrome
 * needs an authenticated identity.
 *
 * Username constraints mirror the server regex
 * (``^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$``); the
 * form lowercases on input so a mixed-case value can't be rejected.
 */

import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { setup as setupRequest } from "@/lib/accountsApi";
import { withBasePath } from "@/lib/basePath";

const MIN_PASSWORD_LENGTH = 8;

export function SetupPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = document.getElementById("setup-username");
    if (el instanceof HTMLInputElement) el.focus();
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (submitting) return;
    setError(null);

    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }

    setSubmitting(true);
    const result = await setupRequest({ username, password });
    if (result.ok) {
      // Hard-navigate so identity.ts re-runs against the new session.
      window.location.href = withBasePath("/");
      return;
    }
    setSubmitting(false);
    // A 409 means someone else just claimed the admin — send them to login.
    if (result.status === 409) {
      window.location.href = withBasePath("/login");
      return;
    }
    setError(result.error);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Create the admin account</h1>
          <p className="text-sm text-muted-foreground">
            First run — pick the username and password for this server's admin. You can invite
            others once you're in.
          </p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="setup-username" className="text-sm font-medium leading-none">
              Username
            </label>
            <Input
              id="setup-username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value.toLowerCase())}
              disabled={submitting}
              required
              pattern="[a-z0-9][a-z0-9._\-]{0,63}(@[a-z0-9.\-]+\.[a-z]{2,})?"
              title="Lowercase letters, digits, dots, hyphens, underscores (or a lowercase email)"
            />
            <p className="text-xs text-muted-foreground">
              Lowercase letters, digits, dots, hyphens, underscores — or a lowercase email.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="setup-password" className="text-sm font-medium leading-none">
              Password
            </label>
            <Input
              id="setup-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
              minLength={MIN_PASSWORD_LENGTH}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="setup-confirm" className="text-sm font-medium leading-none">
              Confirm password
            </label>
            <Input
              id="setup-confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              disabled={submitting}
              required
              minLength={MIN_PASSWORD_LENGTH}
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

          <Button
            type="submit"
            className="w-full"
            disabled={submitting || password.length < MIN_PASSWORD_LENGTH || username.length === 0}
          >
            {submitting ? "Creating…" : "Create admin"}
          </Button>
        </form>
      </div>
    </div>
  );
}
