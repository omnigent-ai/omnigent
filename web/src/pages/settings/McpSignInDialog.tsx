import { useEffect, useRef, useState } from "react";
import { ExternalLinkIcon, Loader2Icon } from "lucide-react";

import { CopyButton, StepNumber } from "@/components/SignInSteps";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Host } from "@/hooks/useHosts";
import {
  cancelMcpLogin,
  fetchMcpLogin,
  isSafeSignInUrl,
  startMcpLogin,
  submitMcpLoginCallback,
  type McpLogin,
} from "@/hooks/useMcpLogin";
import { ApiError } from "@/lib/sessionsApi";

export const MCP_LOGIN_POLL_MS = 1500;
/** Consecutive failed polls tolerated before the dialog gives up. */
export const MAX_POLL_FAILURES = 3;

type Phase =
  { kind: "starting" } | { kind: "pending"; login: McpLogin } | { kind: "error"; message: string };

export interface McpSignInDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  host: Host;
  harness: string;
  harnessLabel: string;
  server: string;
  serverLabel: string;
  plugin?: string;
  sourceId?: string;
  onSignedIn: () => void;
}

function endedMessage(login: McpLogin): string {
  if (login.error) return login.error;
  if (login.state === "expired") return "Sign-in timed out.";
  if (login.state === "cancelled") return "Sign-in was cancelled.";
  return "The sign-in didn't complete.";
}

function requestErrorMessage(error: unknown, hostName: string): string {
  if (!(error instanceof ApiError)) return `Couldn't reach ${hostName}.`;
  if (error.status === 501) return `Update ${hostName} to sign in to MCP servers from here.`;
  if ((error.status < 500 || error.status === 503) && error.message) return error.message;
  return `Couldn't reach ${hostName}.`;
}

/** Runs the harness's own sign-in for one MCP server on the host. */
export function McpSignInDialog({
  open,
  onOpenChange,
  host,
  harness,
  harnessLabel,
  server,
  serverLabel,
  plugin,
  sourceId,
  onSignedIn,
}: McpSignInDialogProps) {
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<Phase>({ kind: "starting" });
  const onSignedInRef = useRef(onSignedIn);
  onSignedInRef.current = onSignedIn;
  const hostId = host.host_id;
  const hostName = host.name;

  useEffect(() => {
    if (!open) return;
    let active = true;
    let loginId: string | null = null;
    let timer: number | undefined;
    let pollFailures = 0;
    const controller = new AbortController();
    setPhase({ kind: "starting" });

    const settle = (login: McpLogin) => {
      if (login.state === "pending") {
        if (!login.authorization_url || !isSafeSignInUrl(login.authorization_url)) {
          void cancelMcpLogin(hostId, login.login_id).catch(() => {});
          loginId = null;
          setPhase({ kind: "error", message: "The host returned an invalid sign-in link." });
          return;
        }
        setPhase({ kind: "pending", login });
        timer = window.setTimeout(poll, MCP_LOGIN_POLL_MS);
        return;
      }
      loginId = null;
      if (login.state === "succeeded") onSignedInRef.current();
      else setPhase({ kind: "error", message: endedMessage(login) });
    };
    const fail = (error: unknown) => {
      if (!active || controller.signal.aborted) return;
      setPhase({ kind: "error", message: requestErrorMessage(error, hostName) });
    };
    const pollFailed = (error: unknown) => {
      if (!active || controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 404) {
        setPhase({ kind: "error", message: "This sign-in ended on the host." });
        return;
      }
      // The sign-in keeps running on the host, so ride out a brief outage.
      pollFailures += 1;
      if (pollFailures < MAX_POLL_FAILURES) timer = window.setTimeout(poll, MCP_LOGIN_POLL_MS);
      else fail(error);
    };
    const poll = () => {
      if (!active || loginId === null) return;
      fetchMcpLogin(hostId, loginId, controller.signal)
        .then((login) => {
          if (!active) return;
          pollFailures = 0;
          settle(login);
        })
        .catch(pollFailed);
    };

    startMcpLogin({ hostId, harness, server, plugin, sourceId })
      .then((login) => {
        if (!active) {
          if (login.state === "pending")
            void cancelMcpLogin(hostId, login.login_id).catch(() => {});
          return;
        }
        loginId = login.login_id;
        settle(login);
      })
      .catch(fail);

    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timer);
      if (loginId !== null) void cancelMcpLogin(hostId, loginId).catch(() => {});
    };
  }, [open, attempt, hostId, hostName, harness, server, plugin, sourceId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-0 overflow-hidden rounded-[20px] bg-background p-0 text-foreground sm:max-w-[440px]">
        <DialogHeader className="px-6 pt-6 pb-4">
          <DialogTitle>Sign in to {serverLabel}</DialogTitle>
          <DialogDescription>
            {harnessLabel} on {hostName} keeps this sign-in, so its sessions can use the server.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-5 px-6 pb-2">
          <PhaseBody phase={phase} hostId={hostId} hostName={hostName} />
        </div>
        <DialogFooter className="mx-0 -mb-0 mt-4 rounded-none border-t border-border px-6 py-4">
          <Button
            variant="outline"
            className="bg-background text-foreground hover:bg-muted dark:bg-background dark:hover:bg-muted"
            onClick={() => onOpenChange(false)}
          >
            {phase.kind === "error" ? "Close" : "Cancel"}
          </Button>
          {phase.kind === "error" && (
            <Button onClick={() => setAttempt((count) => count + 1)}>Try again</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PhaseBody({
  phase,
  hostId,
  hostName,
}: {
  phase: Phase;
  hostId: string;
  hostName: string;
}) {
  if (phase.kind === "starting") {
    return (
      <div role="status" className="flex items-center gap-3 text-sm">
        <Loader2Icon aria-hidden="true" className="size-4 animate-spin" />
        Starting sign-in on {hostName}…
      </div>
    );
  }
  if (phase.kind === "error") {
    return (
      <div
        role="alert"
        className="rounded-xl border border-destructive/30 bg-destructive/5 px-3.5 py-2.5 text-sm text-destructive"
      >
        {phase.message}
      </div>
    );
  }
  return (
    <PendingSteps
      key={phase.login.login_id}
      hostId={hostId}
      hostName={hostName}
      login={phase.login}
    />
  );
}

function PendingSteps({
  hostId,
  hostName,
  login,
}: {
  hostId: string;
  hostName: string;
  login: McpLogin;
}) {
  const [callbackUrl, setCallbackUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const url = login.authorization_url;
  if (!url || !isSafeSignInUrl(url)) {
    return <p role="alert">The host returned an invalid sign-in link.</p>;
  }
  const submit = () => {
    const value = callbackUrl.trim();
    if (!value) return;
    setSubmitting(true);
    setSubmitError(null);
    submitMcpLoginCallback(hostId, login.login_id, value)
      .catch((error: unknown) => setSubmitError(requestErrorMessage(error, hostName)))
      .finally(() => setSubmitting(false));
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-3">
        <StepNumber>1</StepNumber>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <p className="text-sm">Open the sign-in page and approve access.</p>
          <div className="flex flex-wrap gap-2">
            <Button asChild size="sm">
              <a href={url} target="_blank" rel="noreferrer">
                Open sign-in page <ExternalLinkIcon className="size-3.5" />
              </a>
            </Button>
            <CopyButton text={url} label="Copy sign-in link" />
          </div>
        </div>
      </div>
      <div className="flex gap-3">
        <StepNumber>2</StepNumber>
        <form
          className="flex min-w-0 flex-1 flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label htmlFor="mcp-sign-in-callback" className="text-sm">
            If the browser then shows a page that won't load, copy its address and paste it here.
          </label>
          <div className="flex gap-2">
            <input
              id="mcp-sign-in-callback"
              value={callbackUrl}
              onChange={(event) => setCallbackUrl(event.target.value)}
              className="h-8 min-w-0 flex-1 rounded-lg border border-border bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
              placeholder="http://localhost:…"
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              type="submit"
              size="sm"
              variant="outline"
              disabled={!callbackUrl.trim() || submitting}
            >
              Finish
            </Button>
          </div>
          {submitError && (
            <p role="alert" className="text-sm text-destructive">
              {submitError}
            </p>
          )}
        </form>
      </div>
      <div className="flex gap-3">
        <StepNumber>
          <Loader2Icon aria-label="Waiting" className="size-3 animate-spin" />
        </StepNumber>
        <p role="status" className="text-sm">
          Waiting for you to finish in the browser…
        </p>
      </div>
    </div>
  );
}
