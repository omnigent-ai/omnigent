import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckIcon, CircleIcon, CopyIcon, KeyRoundIcon, Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import {
  fetchHosts,
  refreshHarnessesOnHost,
  useInstallHarness,
  type Host,
  type StoreCredentialResult,
} from "@/hooks/useHosts";
import { useHarnessSetupSteps } from "@/lib/agentLabels";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { copyText } from "@/lib/clipboard";
import {
  HARNESS_CREDENTIAL_COPY,
  harnessAuthableOnHost,
  harnessCredentialFamily,
  harnessInstallableOnHost,
  isCodexHarness,
  type HarnessCredentialKind,
} from "@/lib/harnessSetup";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import { ApiError } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { HarnessCredentialForm } from "@/shell/HarnessCredentialForm";

export type HarnessFixMode = "reconfigure" | "update" | "install";

export interface HarnessFixResult {
  configuredHarnesses: Record<string, boolean | string>;
  credentialKind?: HarnessCredentialKind;
  version?: string;
  gatewayInference?: Record<string, boolean> | null;
}

export interface HarnessFixDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: HarnessFixMode;
  host: Host;
  harness: string;
  harnessLabel: string;
  onFixed: (result: HarnessFixResult) => void;
}

type Phase =
  "install" | "choose" | "progress" | "isaac" | "personal" | "credentials" | "setup" | "error";

export function HarnessFixDialog(props: HarnessFixDialogProps) {
  const { open, onOpenChange, mode, host, harness, harnessLabel, onFixed } = props;
  const hostId = host.host_id;
  const hostName = host.name;
  const queryClient = useQueryClient();
  const { mutateAsync: installHarness } = useInstallHarness(hostId);
  const info = useServerInfo();
  const setupSteps = useHarnessSetupSteps();
  const canonicalHarness = canonicalNativeHarness(harness);
  const credentialFamily = harnessCredentialFamily(harness);
  const loginCommand = useMemo(() => {
    if (isCodexHarness(harness)) return "codex login --device-auth";
    if (canonicalHarness === "pi" || canonicalHarness === "pi-native") return null;
    return setupSteps[canonicalHarness]?.find((step) => step.kind === "auth")?.command ?? null;
  }, [canonicalHarness, harness, setupSteps]);
  const isaacSupported = credentialFamily !== null;
  const personalSupported =
    loginCommand !== null &&
    (isCodexHarness(harness) ||
      canonicalHarness === "claude" ||
      canonicalHarness === "claude-native");
  const hasCredentialCards = isaacSupported || personalSupported;
  const canUseCredentialForm = harnessAuthableOnHost(info, harness, host);
  const credentialChoicePhase: Phase = hasCredentialCards
    ? "choose"
    : canUseCredentialForm
      ? "credentials"
      : "setup";
  const installable = harnessInstallableOnHost(info, harness, host);
  const requiresManualSetup = !installable && (mode === "install" || mode === "update");
  const initialPhase: Phase = requiresManualSetup
    ? "setup"
    : mode === "update"
      ? "progress"
      : mode === "install" && !hasCredentialCards
        ? "install"
        : credentialChoicePhase;

  const [phase, setPhase] = useState<Phase>(initialPhase);
  const [choice, setChoice] = useState<HarnessCredentialKind>(
    isaacSupported ? "isaac" : "personal",
  );
  const [error, setError] = useState("");
  const [stillNotReady, setStillNotReady] = useState(false);
  const [updatedNeedsSetup, setUpdatedNeedsSetup] = useState(false);
  const [checking, setChecking] = useState(false);
  const runId = useRef(0);

  const finish = useCallback(
    (
      configuredHarnesses: Record<string, boolean | string>,
      credentialKind?: HarnessCredentialKind,
      version?: string | null,
      announce = true,
      gatewayInference?: Record<string, boolean> | null,
    ) => {
      onFixed({
        configuredHarnesses,
        credentialKind,
        ...(version ? { version } : {}),
        ...(gatewayInference !== undefined ? { gatewayInference } : {}),
      });
      if (announce) {
        showToast(`${harnessLabel}${version ? ` ${version}` : ""} is ready on ${hostName}`);
      }
      onOpenChange(false);
    },
    [harnessLabel, hostName, onFixed, onOpenChange],
  );

  const fail = useCallback((reason: unknown, id: number) => {
    if (runId.current !== id) return;
    setError(reason instanceof Error ? reason.message : "Something went wrong.");
    setPhase("error");
  }, []);

  const authenticate = useCallback((selected: HarnessCredentialKind) => {
    setStillNotReady(false);
    setPhase(selected);
  }, []);

  const needsSetup = useCallback(() => {
    setUpdatedNeedsSetup(mode === "update");
    setPhase(credentialChoicePhase);
  }, [credentialChoicePhase, mode]);

  const start = useCallback(async () => {
    if (requiresManualSetup) {
      setError("");
      setStillNotReady(false);
      setUpdatedNeedsSetup(false);
      setPhase("setup");
      return;
    }
    const id = ++runId.current;
    setError("");
    setStillNotReady(false);
    setUpdatedNeedsSetup(false);
    setPhase("progress");
    try {
      const result = await installHarness(harness);
      if (runId.current !== id) return;
      if (result.configured_harnesses[harness] === true) {
        finish(result.configured_harnesses, undefined, result.version);
        return;
      }
      if (mode === "update") needsSetup();
      else if (hasCredentialCards) authenticate(choice);
      else setPhase(credentialChoicePhase);
    } catch (reason) {
      fail(reason, id);
    }
  }, [
    authenticate,
    choice,
    fail,
    finish,
    harness,
    hasCredentialCards,
    credentialChoicePhase,
    installHarness,
    mode,
    needsSetup,
    requiresManualSetup,
  ]);
  const startRef = useRef(start);
  startRef.current = start;

  useEffect(() => {
    if (!open) return;
    runId.current += 1;
    setChoice(isaacSupported ? "isaac" : "personal");
    setError("");
    setStillNotReady(false);
    setUpdatedNeedsSetup(false);
    setChecking(false);
    setPhase(initialPhase);
    if (mode === "update" && installable) void startRef.current();
    return () => {
      runId.current += 1;
    };
  }, [harness, hostId, initialPhase, installable, isaacSupported, mode, open]);

  const setOpen = (nextOpen: boolean) => {
    if (!nextOpen) runId.current += 1;
    onOpenChange(nextOpen);
  };

  const retry = () => {
    runId.current += 1;
    setError("");
    setStillNotReady(false);
    setUpdatedNeedsSetup(false);
    setPhase(initialPhase);
    if (mode === "update" && installable) void start();
  };

  const refetchReadinessFromHosts = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["hosts"] });
    const hosts = await queryClient.fetchQuery({
      queryKey: ["hosts", { includeSandbox: false }],
      queryFn: () => fetchHosts(false),
      staleTime: 0,
    });
    const refreshed = hosts.find((candidate) => candidate.host_id === hostId);
    if (!refreshed) throw new Error(`${hostName} is no longer connected.`);
    return {
      configured_harnesses: refreshed.configured_harnesses ?? {},
      gateway_inference: refreshed.gateway_inference ?? null,
    };
  }, [hostId, hostName, queryClient]);

  const checkAgain = async () => {
    if (checking) return;
    const id = ++runId.current;
    setChecking(true);
    setStillNotReady(false);
    try {
      let result;
      try {
        result = await refreshHarnessesOnHost(hostId);
      } catch (reason) {
        if (
          reason instanceof ApiError &&
          (reason.code === "refresh_unsupported" || reason.status === 504)
        ) {
          result = await refetchReadinessFromHosts();
        } else {
          throw reason;
        }
      }
      if (runId.current !== id) return;
      if (result.configured_harnesses[harness] === true) {
        const credentialKind: HarnessCredentialKind | undefined = result.gateway_inference?.[
          harness
        ]
          ? "isaac"
          : phase === "personal"
            ? "personal"
            : undefined;
        finish(
          result.configured_harnesses,
          credentialKind,
          undefined,
          true,
          result.gateway_inference,
        );
      } else {
        setStillNotReady(true);
      }
    } catch (reason) {
      fail(reason, id);
    } finally {
      if (runId.current === id) setChecking(false);
    }
  };

  const onCredentialStored = (result: StoreCredentialResult) => {
    finish(result.configured_harnesses, undefined, undefined, false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="gap-0 overflow-hidden rounded-[20px] p-0 sm:max-w-[440px]">
        <DialogHeader className="px-6 pt-6 pb-4">
          <DialogTitle>{dialogTitle(mode, phase, harnessLabel, hostName)}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-5 px-6 pb-2">
          {updatedNeedsSetup && phase !== "progress" && (
            <p role="status" className="text-sm text-amber-700 dark:text-amber-300">
              {harnessLabel} was updated but still needs to be set up on {hostName}.
            </p>
          )}
          {phase === "install" ? (
            <DialogDescription>
              Install {harnessLabel} on {hostName}.
            </DialogDescription>
          ) : phase === "choose" ? (
            <ChooseCredential
              choice={choice}
              onChoice={setChoice}
              hostName={hostName}
              harnessLabel={harnessLabel}
              isaacSupported={isaacSupported}
              personalSupported={personalSupported}
              canUseCredentialForm={mode !== "install" && canUseCredentialForm}
              onUseCredentialForm={() => setPhase("credentials")}
            />
          ) : phase === "error" ? (
            <ErrorState error={error} hostName={hostName} />
          ) : phase === "personal" && loginCommand ? (
            <PersonalCommand
              command={loginCommand}
              harness={harness}
              hostName={hostName}
              stillNotReady={stillNotReady}
            />
          ) : phase === "isaac" ? (
            <IsaacCommand
              harnessLabel={harnessLabel}
              hostName={hostName}
              stillNotReady={stillNotReady}
            />
          ) : phase === "credentials" ? (
            <CredentialForms
              harnessLabel={harnessLabel}
              hostName={hostName}
              harness={harness}
              host={host}
              onDone={onCredentialStored}
            />
          ) : phase === "setup" ? (
            <SetupCommand
              harnessLabel={harnessLabel}
              hostName={hostName}
              stillNotReady={stillNotReady}
            />
          ) : (
            <ProgressState mode={mode} hostName={hostName} harnessLabel={harnessLabel} />
          )}
        </div>

        <DialogFooter className="mx-0 -mb-0 mt-4 rounded-none border-t border-border px-6 py-4">
          {phase === "install" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button onClick={() => void start()}>Install</Button>
            </>
          ) : phase === "choose" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button onClick={() => (mode === "install" ? void start() : authenticate(choice))}>
                {mode === "install" ? "Install" : "Authenticate"}
              </Button>
            </>
          ) : phase === "error" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              <Button onClick={retry}>Try again</Button>
            </>
          ) : phase === "personal" || phase === "isaac" || phase === "setup" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              <Button disabled={checking} onClick={() => void checkAgain()}>
                {checking ? (
                  <>
                    <Loader2Icon className="size-4 animate-spin" />
                    Checking…
                  </>
                ) : (
                  "Check again"
                )}
              </Button>
            </>
          ) : phase === "credentials" ? (
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
          ) : (
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ChooseCredential({
  choice,
  onChoice,
  hostName,
  harnessLabel,
  isaacSupported,
  personalSupported,
  canUseCredentialForm,
  onUseCredentialForm,
}: {
  choice: HarnessCredentialKind;
  onChoice: (choice: HarnessCredentialKind) => void;
  hostName: string;
  harnessLabel: string;
  isaacSupported: boolean;
  personalSupported: boolean;
  canUseCredentialForm: boolean;
  onUseCredentialForm: () => void;
}) {
  return (
    <>
      <DialogDescription>
        Choose the credential {harnessLabel} will use to authenticate on {hostName}.
      </DialogDescription>
      <div role="radiogroup" aria-label="Credential" className="flex flex-col gap-2">
        {isaacSupported && (
          <CredentialCard
            selected={choice === "isaac"}
            title={HARNESS_CREDENTIAL_COPY.isaac.label}
            description={HARNESS_CREDENTIAL_COPY.isaac.description}
            onClick={() => onChoice("isaac")}
          />
        )}
        {personalSupported && (
          <CredentialCard
            selected={choice === "personal"}
            title={HARNESS_CREDENTIAL_COPY.personal.label}
            description={HARNESS_CREDENTIAL_COPY.personal.description}
            onClick={() => onChoice("personal")}
          />
        )}
      </div>
      {canUseCredentialForm && (
        <button
          type="button"
          className="self-start text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          onClick={onUseCredentialForm}
        >
          Use an API key or gateway URL instead
        </button>
      )}
    </>
  );
}

function CredentialCard({
  selected,
  title,
  description,
  onClick,
}: {
  selected: boolean;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onClick}
      className={cn(
        "flex items-center gap-3 rounded-xl border px-3.5 py-3 text-left transition-colors",
        selected ? "border-foreground bg-foreground/5" : "border-border hover:bg-muted/50",
      )}
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
        <KeyRoundIcon className="size-4" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-ui font-medium text-foreground">{title}</span>
        <span className="text-sm text-muted-foreground">{description}</span>
      </span>
      <span
        className={cn(
          "flex size-[18px] shrink-0 items-center justify-center rounded-full border",
          selected ? "border-foreground bg-foreground text-background" : "border-border-strong",
        )}
      >
        {selected && <CheckIcon className="size-3" />}
      </span>
    </button>
  );
}

function ProgressState({
  mode,
  hostName,
  harnessLabel,
}: {
  mode: HarnessFixMode;
  hostName: string;
  harnessLabel: string;
}) {
  const installing =
    mode === "update"
      ? `Installing the latest version on ${hostName}`
      : `Installing ${harnessLabel} on ${hostName}`;
  return (
    <>
      <DialogDescription>
        {mode === "update"
          ? `Updating ${harnessLabel} to the latest version on ${hostName}.`
          : `Installing ${harnessLabel} on ${hostName}.`}{" "}
        This can take a few minutes.
      </DialogDescription>
      <Steps
        rows={[
          [installing, "active"],
          [`Checking ${harnessLabel}`, "pending"],
        ]}
      />
    </>
  );
}

function PersonalCommand({
  command,
  harness,
  hostName,
  stillNotReady,
}: {
  command: string;
  harness: string;
  hostName: string;
  stillNotReady: boolean;
}) {
  return (
    <>
      <DialogDescription>Run this in a terminal on {hostName}, then come back.</DialogDescription>
      <CommandRow command={command} label="Copy login command" />
      <p className="text-sm text-muted-foreground">
        {harness.includes("claude")
          ? "Open the link it prints, then paste the code from your browser back into the terminal."
          : "Open the link it prints and enter the one-time code."}
      </p>
      {stillNotReady && (
        <p role="status" className="text-sm text-destructive">
          Still not signed in on {hostName}.
        </p>
      )}
    </>
  );
}

function IsaacCommand({
  harnessLabel,
  hostName,
  stillNotReady,
}: {
  harnessLabel: string;
  hostName: string;
  stillNotReady: boolean;
}) {
  return (
    <>
      <DialogDescription>Run this in a terminal on {hostName}, then come back.</DialogDescription>
      <CommandRow command="omni setup" label="Copy omni setup" />
      <p className="text-sm text-muted-foreground">
        Choose {harnessLabel}, then the Databricks Unity Gateway credential.
      </p>
      {stillNotReady && (
        <p role="status" className="text-sm text-destructive">
          Still not signed in on {hostName}.
        </p>
      )}
    </>
  );
}

function SetupCommand({
  harnessLabel,
  hostName,
  stillNotReady,
}: {
  harnessLabel: string;
  hostName: string;
  stillNotReady: boolean;
}) {
  return (
    <>
      <DialogDescription>
        Run <code className="font-mono text-foreground">omni setup</code> on {hostName} to finish
        setting up {harnessLabel}.
      </DialogDescription>
      <CommandRow command="omni setup" label="Copy omni setup" />
      {stillNotReady && (
        <p role="status" className="text-sm text-destructive">
          {harnessLabel} still isn’t ready on {hostName}.
        </p>
      )}
    </>
  );
}

function CredentialForms({
  harnessLabel,
  hostName,
  harness,
  host,
  onDone,
}: {
  harnessLabel: string;
  hostName: string;
  harness: string;
  host: Host;
  onDone: (result: StoreCredentialResult) => void;
}) {
  return (
    <>
      <DialogDescription>
        Add a credential {harnessLabel} can use to authenticate on {hostName}.
      </DialogDescription>
      <HarnessCredentialForm
        harness={harness}
        label={harnessLabel}
        host={host}
        command={null}
        onDone={onDone}
      />
    </>
  );
}

function Steps({ rows }: { rows: [string, "active" | "pending"][] }) {
  return (
    <div className="flex flex-col gap-2.5">
      {rows.map(([label, status]) => (
        <div
          key={label}
          className={cn(
            "flex items-center gap-2.5 text-ui",
            status === "pending" && "text-muted-foreground",
          )}
        >
          {status === "active" ? (
            <Loader2Icon aria-label="In progress" className="size-4 animate-spin" />
          ) : (
            <CircleIcon className="size-3 fill-muted text-muted" />
          )}
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function ErrorState({ error, hostName }: { error: string; hostName: string }) {
  return (
    <>
      <DialogDescription>
        Nothing changed on {hostName}. Try again, or run setup in a terminal on that machine.
      </DialogDescription>
      <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-3.5 py-2.5 text-ui text-destructive">
        {error}
      </div>
      <CommandRow command="omni setup" label="Copy omni setup" />
    </>
  );
}

function CommandRow({ command, label }: { command: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex min-h-9 items-center gap-2 rounded-[10px] border border-border px-3 py-1.5">
      <code className="min-w-0 flex-1 break-all font-mono text-sm text-foreground">{command}</code>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          void copyText(command)
            .then(() => {
              setCopied(true);
              showToast("Copied to clipboard.");
            })
            .catch(() => showToast("Couldn't copy — select and copy manually.", { duration: 0 }));
        }}
        aria-label={label}
      >
        {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

function dialogTitle(
  mode: HarnessFixMode,
  phase: Phase,
  harnessLabel: string,
  hostName: string,
): string {
  if (phase === "personal") return `Sign in to ${harnessLabel} on ${hostName}`;
  if (phase === "isaac") return `Set up Isaac for ${harnessLabel} on ${hostName}`;
  if (phase === "setup") return `Set up ${harnessLabel} on ${hostName}`;
  if (phase === "error") {
    if (mode === "update") return `Couldn’t update ${harnessLabel}`;
    if (mode === "install") return `Couldn’t install ${harnessLabel}`;
    return `Couldn’t reconfigure ${harnessLabel}`;
  }
  if (phase === "progress") {
    if (mode === "update") return `Updating ${harnessLabel}…`;
    if (mode === "install") return `Installing ${harnessLabel}…`;
    return `Reconfiguring ${harnessLabel}…`;
  }
  return mode === "install" ? `Install ${harnessLabel}` : `Reconfigure ${harnessLabel}`;
}
