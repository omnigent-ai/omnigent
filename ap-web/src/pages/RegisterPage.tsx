/**
 * The invite-redemption page (``/register?invite=...``).
 *
 * Reached by clicking the copyable URL an admin minted in the
 * Members page. The user chooses their own username + password,
 * the server consumes the invite + creates the account + sets the
 * session cookie, and we navigate to ``/``.
 *
 * Mounted outside the AppShell for the same reason LoginPage is —
 * the chrome loads sidebar / conversations / runner hooks that
 * require an authenticated identity.
 *
 * Username constraints are intentionally restrictive to match the
 * server's validation regex
 * (``^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$``).
 * The form lowercases on input so the user can't accidentally
 * type a mixed-case value that the server then rejects.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { register as registerRequest } from "@/lib/accountsApi";

const MIN_PASSWORD_LENGTH = 8;

export function RegisterPage() {
  const [params] = useSearchParams();
  const invite = params.get("invite") ?? "";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Server rejects invites that are missing / expired / redeemed
  // with a generic 400. Surface the same generic UI in the
  // most-common bad case (no invite param at all).
  const missingInvite = invite === "";

  useEffect(() => {
    const el = document.getElementById("register-username");
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
    const result = await registerRequest({ invite, username, password });
    if (result.ok) {
      window.location.href = "/";
      return;
    }
    setSubmitting(false);
    setError(result.error);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Create your account</h1>
          <p className="text-sm text-muted-foreground">
            You were invited to join this Omnigent server.
          </p>
        </div>

        {missingInvite ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            This page needs an invite token in the URL — make sure you opened the link your admin
            sent you.
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="register-username" className="text-sm font-medium leading-none">
                Username
              </label>
              <Input
                id="register-username"
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
              <label htmlFor="register-password" className="text-sm font-medium leading-none">
                Password
              </label>
              <Input
                id="register-password"
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
              <label htmlFor="register-confirm" className="text-sm font-medium leading-none">
                Confirm password
              </label>
              <Input
                id="register-confirm"
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
              disabled={
                submitting || password.length < MIN_PASSWORD_LENGTH || username.length === 0
              }
            >
              {submitting ? "Creating…" : "Create account"}
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}
