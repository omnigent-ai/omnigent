/**
 * Account-status footer for the sidebar.
 *
 * Renders only when the accounts auth provider is active —
 * gated FIRST on the ``/v1/info`` capabilities probe (so the
 * component is a no-op in any non-accounts deploy without
 * ever hitting ``/auth/me``), THEN on a successful ``/auth/me``
 * call. Inside, shows:
 *
 * - The signed-in username, with an "Admin" badge when applicable.
 * - A link to ``/members`` (only for admins).
 * - A sign-out item that clears the session cookie via
 *   ``POST /auth/logout`` and hard-navigates back to ``/login``.
 *
 * Sits at the bottom of the left sidebar as a full-width row in its
 * own bordered footer block, with the dropdown opening upward. It
 * owns that footer chrome on purpose — the whole thing (border +
 * padding included) disappears when the component gates out, so
 * non-accounts deploys never see an empty bar.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "@/lib/routing";
import {
  KeyboardIcon,
  KeyRoundIcon,
  LogOutIcon,
  ShieldCheckIcon,
  UserCogIcon,
  UsersIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { MOD_KEY, openKeyboardShortcuts } from "@/components/KeyboardShortcutsDialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { changePassword, type CurrentAccount, getMe, logout } from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";

export function AccountMenu() {
  const info = useServerInfo();
  const accountsEnabled = info !== "loading" && info.accounts_enabled;

  const [me, setMe] = useState<CurrentAccount | null | "unknown">("unknown");

  // Change-password dialog state.
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);

  useEffect(() => {
    // Don't even hit /auth/me when accounts is off — saves a
    // request on every page load for the internal hosted product.
    if (!accountsEnabled) return;
    void (async () => {
      const account = await getMe();
      setMe(account);
    })();
  }, [accountsEnabled]);

  const onSignOut = useCallback(async () => {
    await logout();
    // Hard navigation so the chat store / react-query cache reset.
    window.location.href = "/login";
  }, []);

  const resetPwForm = useCallback(() => {
    setOldPw("");
    setNewPw("");
    setConfirmPw("");
    setPwError(null);
    setPwDone(false);
    setPwBusy(false);
  }, []);

  const onSubmitPassword = useCallback(async () => {
    if (newPw !== confirmPw) {
      setPwError("New passwords don't match.");
      return;
    }
    setPwBusy(true);
    setPwError(null);
    const result = await changePassword({ old_password: oldPw, new_password: newPw });
    setPwBusy(false);
    if (result.ok) {
      setPwDone(true);
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
    } else {
      setPwError(result.error);
    }
  }, [oldPw, newPw, confirmPw]);

  // First gate: not in accounts mode → never render. This is the
  // single switch that keeps the chrome identical to a pre-PR-2008
  // build for header / OIDC deploys.
  if (!accountsEnabled) return null;
  // Second gate: probe in flight or failed → render nothing
  // (matches the pre-context-aware behavior).
  if (me === "unknown") return null;
  if (me === null) return null;

  // Footer block at the bottom of the sidebar. No padding — the
  // full-width button reaches the sidebar edges, and its own padding
  // supplies the click target. This wrapper lives behind the same gates
  // as the menu, so the whole footer vanishes when accounts is off.
  // `shrink-0` keeps it at full height so a long conversation list
  // scrolls in the flex-1 <nav> above instead of compressing the footer.
  return (
    <div className="shrink-0">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            // Full-width row that mirrors the "New session" button so the
            // footer reads as part of the sidebar rather than a bolted-on
            // control. `h-auto py-2` overrides the size's fixed height to
            // give a larger click target; truncate keeps long account ids
            // from blowing out the fixed-width panel.
            className="h-auto w-full justify-start gap-2 px-3 py-2"
          >
            {/* Bordered box around the icon — an avatar-style frame that
                anchors the account row visually. */}
            <span className="flex size-6 shrink-0 items-center justify-center rounded-md border border-border">
              <UserCogIcon className="size-3.5" />
            </span>
            <span className="min-w-0 flex-1 truncate text-left">{me.id}</span>
          </Button>
        </DropdownMenuTrigger>
        {/* The footer sits at the bottom of the viewport, so open the menu
            upward and align it to the start edge. `w-auto` overrides the
            base trigger-width binding (the trigger is the full-width 256px
            row) so the popover sizes to its content down to the min-w-48
            floor; `mx-2` insets it from the sidebar edges. */}
        <DropdownMenuContent align="start" side="top" className="mx-2 w-auto min-w-48">
          <DropdownMenuLabel>
            {me.id}
            {me.is_admin && (
              <span className="ml-1 text-xs font-normal text-muted-foreground">(admin)</span>
            )}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          {me.is_admin && (
            <>
              <DropdownMenuItem asChild>
                <Link to="/members" className="flex items-center gap-2">
                  <UsersIcon /> Members
                </Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link to="/policies" className="flex items-center gap-2">
                  <ShieldCheckIcon /> Policies
                </Link>
              </DropdownMenuItem>
            </>
          )}
          <DropdownMenuItem
            onClick={() => openKeyboardShortcuts()}
            className="flex items-center gap-2"
          >
            <KeyboardIcon /> Keyboard shortcuts
            <DropdownMenuShortcut>{MOD_KEY} /</DropdownMenuShortcut>
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => {
              resetPwForm();
              setPwOpen(true);
            }}
            className="flex items-center gap-2"
          >
            <KeyRoundIcon /> Change password
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => void onSignOut()} className="flex items-center gap-2">
            <LogOutIcon /> Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog
        open={pwOpen}
        onOpenChange={(open) => {
          setPwOpen(open);
          if (!open) resetPwForm();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change password</DialogTitle>
            <DialogDescription>
              {pwDone
                ? "Your password has been changed."
                : "Enter your current password and choose a new one."}
            </DialogDescription>
          </DialogHeader>

          {!pwDone && (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                void onSubmitPassword();
              }}
            >
              <Input
                type="password"
                autoComplete="current-password"
                placeholder="Current password"
                value={oldPw}
                onChange={(e) => setOldPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="New password"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="Confirm new password"
                value={confirmPw}
                onChange={(e) => setConfirmPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              {pwError !== null && (
                <div
                  role="alert"
                  className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                >
                  {pwError}
                </div>
              )}
              <DialogFooter>
                <Button
                  type="submit"
                  disabled={
                    pwBusy || oldPw.length === 0 || newPw.length === 0 || confirmPw.length === 0
                  }
                >
                  {pwBusy ? "Changing…" : "Change password"}
                </Button>
              </DialogFooter>
            </form>
          )}

          {pwDone && (
            <DialogFooter>
              <Button onClick={() => setPwOpen(false)}>Done</Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
