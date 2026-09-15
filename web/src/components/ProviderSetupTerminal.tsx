import {
  CircleAlertIcon,
  CircleCheckIcon,
  CircleXIcon,
  Loader2Icon,
  RotateCwIcon,
  SquareIcon,
  TerminalIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { type ConnectionState, TerminalSession } from "@/components/blocks/TerminalSession";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  cancelSetupOperation,
  fetchSetupOperation,
  setupOperationAttachUrl,
  type SetupOperation,
  verifySetupOperation,
} from "@/lib/providerSetupApi";

/** Operation states where the setup process can still produce new output. */
const ACTIVE_STATES = new Set<SetupOperation["state"]>(["pending", "running"]);

function isActive(operation: SetupOperation): boolean {
  return ACTIVE_STATES.has(operation.state);
}

function statusDetails(operation: SetupOperation): {
  label: string;
  tone: string;
  icon: typeof Loader2Icon;
} {
  switch (operation.state) {
    case "pending":
      return {
        label: "Waiting to start",
        tone: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
        icon: Loader2Icon,
      };
    case "running":
      return {
        label: "Running",
        tone: "border-info/30 bg-info/10 text-info",
        icon: Loader2Icon,
      };
    case "succeeded":
      return {
        label: "Completed",
        tone: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        icon: CircleCheckIcon,
      };
    case "failed":
      return {
        label: "Failed",
        tone: "border-destructive/30 bg-destructive/10 text-destructive",
        icon: CircleAlertIcon,
      };
    case "cancelled":
      return {
        label: "Cancelled",
        tone: "border-muted-foreground/30 bg-muted text-muted-foreground",
        icon: CircleXIcon,
      };
    case "expired":
      return {
        label: "Expired",
        tone: "border-muted-foreground/30 bg-muted text-muted-foreground",
        icon: CircleAlertIcon,
      };
  }
}

function stateMessage(operation: SetupOperation): string {
  switch (operation.state) {
    case "pending":
      return "Preparing the connection…";
    case "running":
      return "Complete the requested step, then return here.";
    case "succeeded":
      return operation.action === "antigravity-login"
        ? "Connection confirmed."
        : "Setup command completed.";
    case "failed":
      return operation.error
        ? "The setup command did not complete."
        : operation.exit_code === null
          ? "The setup command failed."
          : `The setup command exited with code ${operation.exit_code}.`;
    case "cancelled":
      return "The setup command was cancelled.";
    case "expired":
      return "The setup operation timed out and was stopped. Start the setup again to continue.";
  }
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export interface ProviderSetupTerminalProps {
  title?: string;
  hostId: string;
  operation: SetupOperation;
  onOperationChange: (next: SetupOperation) => void;
  onFinished: () => void;
}

/**
 * Interactive terminal for a host-scoped provider setup operation.
 *
 * The terminal transport is separate from operation polling: a terminal can
 * close after a successful command, while the API remains the source of truth
 * for whether the command completed, failed, or was cancelled.
 */
export function ProviderSetupTerminal({
  title,
  hostId,
  operation,
  onOperationChange,
  onFinished,
}: ProviderSetupTerminalProps) {
  const resolvedTheme = useResolvedThemeMode();
  const [terminalNode, setTerminalNode] = useState<HTMLDivElement | null>(null);
  const [bridgeState, setBridgeState] = useState<ConnectionState>({ kind: "connecting" });
  const [attachAttempt, setAttachAttempt] = useState(0);
  const [cancelPending, setCancelPending] = useState(false);
  const [verifyPending, setVerifyPending] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [showOutput, setShowOutput] = useState(() => operation.state !== "succeeded");
  const hasLiveOutputRef = useRef(isActive(operation));
  const sessionRef = useRef<TerminalSession | null>(null);
  const finishedOperationIdRef = useRef<string | null>(null);
  const onOperationChangeRef = useRef(onOperationChange);
  const onFinishedRef = useRef(onFinished);

  onOperationChangeRef.current = onOperationChange;
  onFinishedRef.current = onFinished;

  const notifyFinished = useCallback((finishedOperationId: string) => {
    if (finishedOperationIdRef.current === finishedOperationId) return;
    finishedOperationIdRef.current = finishedOperationId;
    onFinishedRef.current();
  }, []);

  const operationId = operation.operation_id;
  const active = isActive(operation);
  const isDark = resolvedTheme === "dark";
  const details = statusDetails(operation);
  const StatusIcon = details.icon;

  useEffect(() => {
    if (operation.state === "succeeded") setShowOutput(false);
  }, [operation.state]);

  useEffect(() => {
    if (!terminalNode || !hasLiveOutputRef.current) return;

    // Clear a previous xterm DOM tree before mounting a replacement. This is
    // needed when the user retries after a bridge error or opens another setup
    // operation in the same dialog.
    terminalNode.replaceChildren();
    setBridgeState({ kind: "connecting" });
    let session: TerminalSession | null = null;
    try {
      session = new TerminalSession(
        terminalNode,
        setupOperationAttachUrl(hostId, operationId),
        setBridgeState,
        isDark,
      );
      sessionRef.current = session;
    } catch (error) {
      setBridgeState({ kind: "error" });
      setRequestError(
        (current) => current ?? errorMessage(error, "Couldn't attach the setup terminal."),
      );
    }

    return () => {
      session?.dispose();
      if (sessionRef.current === session) sessionRef.current = null;
    };
    // Theme changes are pushed into the existing xterm instance below; they
    // must not create a second WebSocket attach.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [terminalNode, hostId, operationId, attachAttempt]);

  useEffect(() => {
    sessionRef.current?.setTheme(isDark);
  }, [isDark]);

  useEffect(() => {
    if (!active) return;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    const poll = async () => {
      try {
        const next = await fetchSetupOperation(hostId, operationId, controller.signal);
        if (disposed) return;
        setRequestError(null);
        onOperationChangeRef.current(next);
        if (isActive(next)) {
          timer = setTimeout(poll, 1200);
        } else {
          notifyFinished(next.operation_id);
        }
      } catch (error) {
        if (disposed || controller.signal.aborted) return;
        setRequestError(errorMessage(error, "Couldn't refresh setup status."));
        // A temporary request failure should not make an active operation look
        // finished. Keep polling at a calm cadence until it answers again.
        timer = setTimeout(poll, 2000);
      }
    };
    timer = setTimeout(poll, 1200);

    return () => {
      disposed = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [active, hostId, notifyFinished, operationId]);

  useEffect(() => {
    if (!active) notifyFinished(operationId);
  }, [active, notifyFinished, operationId]);

  const cancel = async () => {
    setCancelPending(true);
    setRequestError(null);
    try {
      const next = await cancelSetupOperation(hostId, operationId);
      onOperationChangeRef.current(next);
      if (!isActive(next)) notifyFinished(next.operation_id);
    } catch (error) {
      setRequestError(errorMessage(error, "Couldn't cancel the setup operation."));
    } finally {
      setCancelPending(false);
    }
  };

  const retryAttach = () => {
    setRequestError(null);
    setAttachAttempt((attempt) => attempt + 1);
  };

  const verify = async () => {
    setVerifyPending(true);
    setVerifyError(null);
    try {
      const next = await verifySetupOperation(hostId, operationId);
      onOperationChangeRef.current(next);
      if (!isActive(next)) notifyFinished(next.operation_id);
    } catch (error) {
      const status = typeof error === "object" && error && "status" in error ? error.status : null;
      setVerifyError(
        status === 409
          ? "Connection not detected yet. Finish signing in, then check again."
          : errorMessage(error, "Couldn't check the connection."),
      );
    } finally {
      setVerifyPending(false);
    }
  };

  const bridgeFailed = active && (bridgeState.kind === "error" || bridgeState.kind === "closed");

  return (
    <section
      aria-label="Provider setup terminal"
      data-operation-id={operationId}
      data-operation-state={operation.state}
      className="overflow-hidden rounded-xl border border-border bg-card shadow-sm"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-muted/30 px-3 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-background text-muted-foreground shadow-xs">
            <TerminalIcon className="size-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">
              {title ?? operation.action.replaceAll("-", " ")}
            </p>
          </div>
        </div>
        <Badge variant="outline" className={`ml-auto gap-1.5 ${details.tone}`}>
          <StatusIcon
            className={details.icon === Loader2Icon ? "size-3 animate-spin" : "size-3"}
            aria-hidden="true"
          />
          {details.label}
        </Badge>
        {active && (
          <Button
            type="button"
            size="xs"
            variant="outline"
            onClick={cancel}
            loading={cancelPending}
            disabled={cancelPending}
            componentId="provider-setup.cancel"
          >
            <SquareIcon className="size-3" aria-hidden="true" />
            Cancel
          </Button>
        )}
        {operation.can_verify === true && (
          <Button
            type="button"
            size="xs"
            variant="outline"
            onClick={verify}
            loading={verifyPending}
            disabled={verifyPending || cancelPending}
            componentId="provider-setup.verify"
          >
            Check connection
          </Button>
        )}
      </header>

      <div
        className="border-b border-border px-3 py-2 text-sm text-muted-foreground"
        aria-live="polite"
      >
        {stateMessage(operation)}
        {active && (
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer">Sign-in help</summary>
            <p className="mt-1">
              On a remote computer, a localhost redirect opens in this browser rather than on the
              selected computer. Use device-code sign-in when available, or sign in in a browser on
              that computer.
            </p>
          </details>
        )}
        {operation.error && <p className="mt-1 text-destructive">{operation.error}</p>}
        {verifyError && <p className="mt-1 text-destructive">{verifyError}</p>}
        {requestError && <p className="mt-1 text-destructive">{requestError}</p>}
      </div>

      {hasLiveOutputRef.current && (
        <div className={showOutput ? "relative h-56 bg-card p-1 sm:h-64" : "hidden"}>
          <div ref={setTerminalNode} className="h-full w-full overflow-hidden" />
          {active && bridgeState.kind === "connecting" && (
            <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-background/70 text-sm text-muted-foreground backdrop-blur-[1px]">
              <Loader2Icon className="mr-2 size-4 animate-spin" aria-hidden="true" />
              Connecting terminal…
            </div>
          )}
          {bridgeFailed && (
            <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 bg-background/85 px-4 text-center text-sm text-muted-foreground backdrop-blur-[1px]">
              <span>
                {bridgeState.kind === "closed"
                  ? `Terminal bridge closed${bridgeState.reason ? `: ${bridgeState.reason}` : "."}`
                  : "Terminal bridge could not connect."}
              </span>
              <Button
                type="button"
                size="xs"
                variant="outline"
                onClick={retryAttach}
                componentId="provider-setup.retry-terminal"
              >
                <RotateCwIcon className="size-3" aria-hidden="true" />
                Retry terminal
              </Button>
            </div>
          )}
        </div>
      )}
      {!showOutput && hasLiveOutputRef.current && (
        <div className="flex items-center justify-between gap-3 px-3 py-2 text-xs text-muted-foreground">
          <span>Command output is hidden.</span>
          <Button type="button" size="xs" variant="ghost" onClick={() => setShowOutput(true)}>
            View output
          </Button>
        </div>
      )}
    </section>
  );
}
