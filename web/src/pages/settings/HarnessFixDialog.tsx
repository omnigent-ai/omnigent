import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  CheckIcon,
  CircleIcon,
  CopyIcon,
  ExternalLinkIcon,
  KeyRoundIcon,
  Loader2Icon,
} from "lucide-react";

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
  fetchHarnessLogin,
  isSecureVerificationUrl,
  cancelHarnessLogin,
  logoutHarnessOnHost,
  refreshHarnessesOnHost,
  startHarnessLogin,
  useDetectedCredentials,
  useInstallHarness,
  type HarnessAccount,
  type HarnessLoginStart,
  type HarnessLogoutResult,
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
  type HarnessCredentialKind,
} from "@/lib/harnessSetup";
import { canonicalNativeHarness, harnessAccountKey } from "@/lib/nativeHarnessModes";
import { ApiError } from "@/lib/sessionsApi";
import type { HarnessFixMode, HarnessFixResult } from "@/lib/harnessFix";
import { cn } from "@/lib/utils";
import { HarnessCredentialForm } from "@/shell/HarnessCredentialForm";

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
  | "install"
  | "choose"
  | "confirm"
  | "progress"
  | "isaac"
  | "personal"
  | "device-error"
  | "credentials"
  | "setup"
  | "error";

type DeviceLoginState =
  { kind: "code"; login: HarnessLoginStart } | { kind: "resuming"; loginId: string } | null;

interface FinishOptions {
  configuredHarnesses: Record<string, boolean | string>;
  credentialKind?: HarnessCredentialKind;
  version?: string | null;
  announce?: boolean;
  gatewayInference?: Record<string, boolean> | null;
  harnessAccounts?: Record<string, HarnessAccount> | null;
  successMessage?: string;
}

export function HarnessFixDialog(props: HarnessFixDialogProps) {
  const { open, onOpenChange, mode, host, harness, harnessLabel, onFixed } = props;
  const hostId = host.host_id;
  const hostName = host.name;
  const queryClient = useQueryClient();
  const { mutateAsync: installHarness } = useInstallHarness(hostId);
  const info = useServerInfo();
  const setupSteps = useHarnessSetupSteps();
  const canonicalHarness = canonicalNativeHarness(harness);
  const accountKey = harnessAccountKey(harness);
  const credentialFamily = harnessCredentialFamily(harness);
  const detectedCredentials = useDetectedCredentials(hostId, open && credentialFamily !== null);
  const loginCommand = useMemo(() => {
    if (accountKey === "codex") return "codex login --device-auth";
    if (accountKey === "pi") return null;
    const configuredCommand = setupSteps[canonicalHarness]?.find(
      (step) => step.kind === "auth",
    )?.command;
    if (accountKey === "claude") return configuredCommand ?? "claude auth login --claudeai";
    return configuredCommand ?? null;
  }, [accountKey, canonicalHarness, setupSteps]);
  const isaacSupported =
    credentialFamily !== null &&
    (host.harness_accounts_supported !== true ||
      detectedCredentials.isError ||
      detectedCredentials.data?.some((credential) => credential.family === "databricks") === true);
  const credentialDecisionPending = credentialFamily !== null && detectedCredentials.isPending;
  const personalSupported =
    loginCommand !== null && (accountKey === "codex" || accountKey === "claude");
  const hasCredentialCards = isaacSupported || personalSupported;
  const canUseCredentialForm = harnessAuthableOnHost(info, harness, host);
  const credentialChoicePhase: Phase =
    credentialDecisionPending || hasCredentialCards
      ? "choose"
      : canUseCredentialForm
        ? "credentials"
        : "setup";
  const installable = harnessInstallableOnHost(info, harness, host);
  const requiresManualSetup = !installable && (mode === "install" || mode === "update");
  const initialPhase: Phase =
    mode === "switch"
      ? "confirm"
      : requiresManualSetup
        ? "setup"
        : mode === "update"
          ? "progress"
          : mode === "install" && !hasCredentialCards
            ? "install"
            : credentialChoicePhase;

  const [phase, setPhase] = useState<Phase>(initialPhase);
  const [choice, setChoice] = useState<HarnessCredentialKind>("personal");
  const [error, setError] = useState("");
  const [stillNotReady, setStillNotReady] = useState(false);
  const [updatedNeedsSetup, setUpdatedNeedsSetup] = useState(false);
  const [checking, setChecking] = useState(false);
  const account = host.harness_accounts?.[accountKey];
  const [emailHint, setEmailHint] = useState("");
  const [deviceLogin, setDeviceLogin] = useState<DeviceLoginState>(null);
  const [secondsRemaining, setSecondsRemaining] = useState(0);
  const runId = useRef(0);
  const activeLogin = useRef<string | null>(null);
  const pollAbort = useRef<AbortController | null>(null);
  const credentialDefaultSettled = useRef(false);
  const initialPhaseRef = useRef(initialPhase);
  initialPhaseRef.current = initialPhase;
  const installableRef = useRef(installable);
  installableRef.current = installable;

  const finish = useCallback(
    ({
      configuredHarnesses,
      credentialKind,
      version,
      announce = true,
      gatewayInference,
      harnessAccounts,
      successMessage,
    }: FinishOptions) => {
      onFixed({
        configuredHarnesses,
        credentialKind,
        ...(version ? { version } : {}),
        ...(gatewayInference !== undefined ? { gatewayInference } : {}),
        ...(harnessAccounts !== undefined ? { harnessAccounts } : {}),
      });
      if (announce) {
        showToast(
          successMessage ??
            `${harnessLabel}${version ? ` ${version}` : ""} is ready on ${hostName}`,
        );
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

  const startDeviceLogin = useCallback(async () => {
    const id = ++runId.current;
    setError("");
    setDeviceLogin(null);
    setPhase("progress");
    if (host.harness_login_supported !== true) {
      setPhase("personal");
      return;
    }
    try {
      const result = await startHarnessLogin(hostId, harness);
      const loginId = result.kind === "busy" ? result.login_id : result.login_id;
      if (runId.current !== id) {
        void cancelHarnessLogin(hostId, harness, loginId).catch(() => undefined);
        return;
      }
      activeLogin.current = loginId;
      if (result.kind === "busy") {
        setDeviceLogin({ kind: "resuming", loginId: result.login_id });
        return;
      }
      setDeviceLogin({ kind: "code", login: result });
      setSecondsRemaining(Math.max(0, Math.ceil((result.expires_at * 1000 - Date.now()) / 1000)));
    } catch (reason) {
      if (runId.current !== id) return;
      if (
        reason instanceof ApiError &&
        reason.status === 409 &&
        reason.code === "login_unsupported"
      ) {
        setPhase("personal");
        return;
      }
      setError(reason instanceof Error ? reason.message : "Couldn’t start sign-in.");
      setPhase("device-error");
    }
  }, [harness, host.harness_login_supported, hostId]);

  const authenticate = useCallback(
    (selected: HarnessCredentialKind) => {
      setStillNotReady(false);
      if (selected === "personal" && accountKey === "codex") {
        void startDeviceLogin();
      } else {
        setPhase(selected);
      }
    },
    [accountKey, startDeviceLogin],
  );

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
        finish({
          configuredHarnesses: result.configured_harnesses,
          version: result.version,
        });
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
    credentialDefaultSettled.current = false;
    setChoice("personal");
    setError("");
    setStillNotReady(false);
    setUpdatedNeedsSetup(false);
    setChecking(false);
    setEmailHint("");
    setDeviceLogin(null);
    setPhase(initialPhaseRef.current);
    if (mode === "update" && installableRef.current) void startRef.current();
    return () => {
      runId.current += 1;
      const loginId = activeLogin.current;
      activeLogin.current = null;
      if (loginId) void cancelHarnessLogin(hostId, harness, loginId).catch(() => undefined);
    };
  }, [harness, hostId, mode, open]);

  useEffect(() => {
    if (
      !open ||
      phase !== "choose" ||
      credentialDecisionPending ||
      credentialDefaultSettled.current
    ) {
      return;
    }
    credentialDefaultSettled.current = true;
    setChoice(isaacSupported ? "isaac" : "personal");
    if (!hasCredentialCards) setPhase(canUseCredentialForm ? "credentials" : "setup");
  }, [
    canUseCredentialForm,
    credentialDecisionPending,
    hasCredentialCards,
    isaacSupported,
    open,
    phase,
  ]);

  const setOpen = (nextOpen: boolean) => {
    if (!nextOpen) runId.current += 1;
    if (!nextOpen && activeLogin.current) {
      const loginId = activeLogin.current;
      activeLogin.current = null;
      void cancelHarnessLogin(hostId, harness, loginId).catch(() => undefined);
    }
    onOpenChange(nextOpen);
  };

  const retry = () => {
    runId.current += 1;
    setError("");
    setDeviceLogin(null);
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
      harness_accounts: refreshed.harness_accounts,
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
        finish({
          configuredHarnesses: result.configured_harnesses,
          credentialKind,
          gatewayInference: result.gateway_inference,
          harnessAccounts: result.harness_accounts,
        });
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
    finish({
      configuredHarnesses: result.configured_harnesses,
      announce: false,
      harnessAccounts: result.harness_accounts,
    });
  };
  const personalCommand =
    mode === "switch" && accountKey === "claude" && emailHint.trim()
      ? `${loginCommand ?? "claude auth login --claudeai"} --email ${shellArgument(emailHint.trim())}`
      : loginCommand;

  const updateCachedHostAfterLogout = useCallback(
    (result: HarnessLogoutResult) => {
      queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (cachedHosts) =>
        cachedHosts?.map((cachedHost) =>
          cachedHost.host_id === hostId
            ? {
                ...cachedHost,
                ...(result.configured_harnesses !== null
                  ? { configured_harnesses: result.configured_harnesses }
                  : {}),
                ...(result.harness_accounts !== null
                  ? { harness_accounts: result.harness_accounts }
                  : {}),
              }
            : cachedHost,
        ),
      );
    },
    [hostId, queryClient],
  );

  const switchAccount = async () => {
    const id = ++runId.current;
    setError("");
    setPhase("progress");
    try {
      const result = await logoutHarnessOnHost(hostId, harness);
      if (runId.current !== id) return;
      updateCachedHostAfterLogout(result);
      if (accountKey === "codex") await startDeviceLogin();
      else setPhase("personal");
    } catch (reason) {
      if (runId.current !== id) return;
      if (
        reason instanceof ApiError &&
        reason.status === 409 &&
        reason.code === "login_unsupported"
      ) {
        setPhase("personal");
        return;
      }
      fail(reason, id);
    }
  };

  useEffect(() => {
    if (deviceLogin?.kind !== "code") return;
    const tick = window.setInterval(() => {
      setSecondsRemaining(
        Math.max(0, Math.ceil((deviceLogin.login.expires_at * 1000 - Date.now()) / 1000)),
      );
    }, 1000);
    return () => window.clearInterval(tick);
  }, [deviceLogin]);

  useEffect(() => {
    if (!deviceLogin) return;
    const loginId = deviceLogin.kind === "code" ? deviceLogin.login.login_id : deviceLogin.loginId;
    const id = runId.current;
    let stopped = false;
    let timer: number | undefined;
    const controller = new AbortController();
    pollAbort.current?.abort();
    pollAbort.current = controller;
    const poll = async () => {
      try {
        const result = await fetchHarnessLogin(hostId, harness, loginId, controller.signal);
        if (stopped || runId.current !== id) return;
        if (result.status === "pending") {
          timer = window.setTimeout(() => void poll(), 2000);
          return;
        }
        activeLogin.current = null;
        if (result.status === "succeeded") {
          let configuredHarnesses = result.configured_harnesses;
          let harnessAccounts = result.harness_accounts;
          if (!configuredHarnesses) {
            const refreshed = await refreshHarnessesOnHost(hostId);
            if (stopped || runId.current !== id) return;
            configuredHarnesses = refreshed.configured_harnesses;
            harnessAccounts = refreshed.harness_accounts;
          }
          const signedIn = harnessAccounts?.[accountKey];
          const identity = [signedIn?.email, signedIn?.plan].filter(Boolean).join(" · ");
          setDeviceLogin(null);
          finish({
            configuredHarnesses,
            credentialKind: "personal",
            harnessAccounts,
            successMessage: identity
              ? `${harnessLabel} is ready on ${hostName}. Signed in as ${identity}.`
              : `${harnessLabel} is ready on ${hostName}`,
          });
          return;
        }
        setDeviceLogin(null);
        setError(result.error || `Sign-in ${result.status}.`);
        setPhase("device-error");
      } catch (reason) {
        if (
          !stopped &&
          runId.current === id &&
          !(reason instanceof DOMException && reason.name === "AbortError")
        ) {
          setDeviceLogin(null);
          setError(reason instanceof Error ? reason.message : "Couldn’t check sign-in.");
          setPhase("device-error");
        }
      }
    };
    timer = window.setTimeout(() => void poll(), 2000);
    return () => {
      stopped = true;
      controller.abort();
      if (pollAbort.current === controller) pollAbort.current = null;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [accountKey, deviceLogin, finish, harness, harnessLabel, hostId, hostName]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="gap-0 overflow-hidden rounded-[20px] bg-background p-0 text-foreground sm:max-w-[440px]">
        <DialogHeader className="px-6 pt-6 pb-4">
          <DialogTitle>
            {dialogTitle(mode, phase, deviceLogin !== null, harnessLabel, hostName)}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-5 px-6 pb-2">
          {updatedNeedsSetup && phase !== "progress" && (
            <p role="status" className="text-sm text-amber-700 dark:text-amber-300">
              {harnessLabel} was updated but still needs to be set up on {hostName}.
            </p>
          )}
          {phase === "confirm" ? (
            <SwitchAccountConfirm
              account={account}
              hostName={hostName}
              emailHint={emailHint}
              onEmailHint={setEmailHint}
            />
          ) : phase === "install" ? (
            <DialogDescription>
              Install {harnessLabel} on {hostName}.
            </DialogDescription>
          ) : phase === "choose" && credentialDecisionPending ? (
            <CredentialDetectionState hostName={hostName} />
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
          ) : deviceLogin?.kind === "code" ? (
            <DeviceCodeState
              login={deviceLogin.login}
              secondsRemaining={secondsRemaining}
              hostName={hostName}
            />
          ) : deviceLogin?.kind === "resuming" ? (
            <DeviceLoginResumeState hostName={hostName} />
          ) : phase === "device-error" ? (
            <DeviceCodeError error={error} hostName={hostName} />
          ) : phase === "personal" && personalCommand ? (
            <PersonalCommand
              command={personalCommand}
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
          {phase === "confirm" ? (
            <>
              <Button
                variant="outline"
                className="bg-background text-foreground hover:bg-muted dark:bg-background dark:hover:bg-muted"
                onClick={() => setOpen(false)}
              >
                Cancel
              </Button>
              <Button onClick={() => void switchAccount()}>Sign out and switch</Button>
            </>
          ) : phase === "install" ? (
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
              <Button
                disabled={credentialDecisionPending}
                onClick={() => (mode === "install" ? void start() : authenticate(choice))}
              >
                {credentialDecisionPending ? (
                  <>
                    <Loader2Icon className="size-4 animate-spin" />
                    Checking…
                  </>
                ) : mode === "install" ? (
                  "Install"
                ) : (
                  "Authenticate"
                )}
              </Button>
            </>
          ) : phase === "error" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              <Button onClick={retry}>Try again</Button>
            </>
          ) : deviceLogin ? (
            <>
              <span className="mr-auto text-xs text-muted-foreground">
                Signing in on {hostName}
              </span>
              <Button
                variant="outline"
                className="bg-background text-foreground hover:bg-muted dark:bg-background dark:hover:bg-muted"
                onClick={() => setOpen(false)}
              >
                Cancel
              </Button>
            </>
          ) : phase === "device-error" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              <Button onClick={() => void startDeviceLogin()}>Try again</Button>
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

function SwitchAccountConfirm({
  account,
  hostName,
  emailHint,
  onEmailHint,
}: {
  account?: HarnessAccount;
  hostName: string;
  emailHint: string;
  onEmailHint: (value: string) => void;
}) {
  const current =
    [account?.email, account?.plan].filter(Boolean).join(" · ") || "the current account";
  return (
    <>
      <DialogDescription>
        {hostName} is signed in as <span className="font-medium text-foreground">{current}</span>.
      </DialogDescription>
      <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm">
        Switching signs this machine out of that account. Every session on {hostName} that uses this
        harness will use the new account. Running sessions keep working until their token expires.
      </div>
      <label className="flex flex-col gap-1.5 text-sm">
        <span className="text-muted-foreground">Account to sign in with (optional)</span>
        <input
          value={emailHint}
          onChange={(event) => onEmailHint(event.target.value)}
          type="email"
          className="h-9 rounded-lg border border-border bg-background px-3 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          placeholder="name@example.com"
          aria-label="Account to sign in with"
        />
      </label>
    </>
  );
}

function DeviceCodeState({
  login,
  secondsRemaining,
  hostName,
}: {
  login: HarnessLoginStart;
  secondsRemaining: number;
  hostName: string;
}) {
  if (!isSecureVerificationUrl(login.verification_url)) {
    return <p role="alert">The host returned an invalid sign-in link.</p>;
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-3">
        <StepNumber>1</StepNumber>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <p className="text-sm">Open the ChatGPT sign-in page and choose the account for Codex.</p>
          <div className="flex flex-wrap gap-2">
            <Button asChild size="sm">
              <a href={login.verification_url} target="_blank" rel="noreferrer">
                Open sign-in page <ExternalLinkIcon className="size-3.5" />
              </a>
            </Button>
            <CopyButton text={login.verification_url} label="Copy sign-in link" />
          </div>
        </div>
      </div>
      <div className="flex gap-3">
        <StepNumber>2</StepNumber>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <p className="text-sm">Enter this one-time code when asked:</p>
          <div className="flex items-center gap-2 rounded-xl border border-dashed border-border bg-muted/40 px-4 py-3">
            <code className="min-w-0 flex-1 text-center font-mono text-xl font-semibold tracking-[0.16em]">
              {login.user_code}
            </code>
            <CopyButton text={login.user_code} label="Copy one-time code" iconOnly />
          </div>
        </div>
      </div>
      <div className="flex gap-3">
        <StepNumber>
          <Loader2Icon aria-label="Waiting" className="size-3 animate-spin" />
        </StepNumber>
        <p className="text-sm">
          Waiting for you to finish in the browser…{" "}
          <span className="text-muted-foreground">
            Code expires in {formatCountdown(secondsRemaining)}
          </span>
        </p>
      </div>
      <p className="sr-only">Signing in to Codex on {hostName}</p>
    </div>
  );
}

function DeviceLoginResumeState({ hostName }: { hostName: string }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-muted/40 px-4 py-3">
      <Loader2Icon aria-label="Waiting" className="size-4 shrink-0 animate-spin" />
      <p className="text-sm text-foreground">
        Another sign-in is already in progress on {hostName}. Waiting for it to finish…
      </p>
    </div>
  );
}

function DeviceCodeError({ error, hostName }: { error: string; hostName: string }) {
  return (
    <>
      <DialogDescription>Codex is still signed out on {hostName}.</DialogDescription>
      <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-3.5 py-2.5 text-sm text-destructive">
        {error || "The sign-in did not complete."}
      </div>
    </>
  );
}

function StepNumber({ children }: { children: React.ReactNode }) {
  return (
    <span className="flex size-5 shrink-0 items-center justify-center rounded-full border border-border bg-muted text-xs">
      {children}
    </span>
  );
}

function CopyButton({
  text,
  label,
  iconOnly = false,
}: {
  text: string;
  label: string;
  iconOnly?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="outline"
      size={iconOnly ? "icon-sm" : "sm"}
      aria-label={label}
      onClick={() => {
        void copyText(text)
          .then(() => setCopied(true))
          .catch(() => showToast("Couldn't copy — select and copy manually.", { duration: 0 }));
      }}
    >
      {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
      {!iconOnly && (copied ? "Copied" : "Copy link")}
    </Button>
  );
}

function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function shellArgument(value: string): string {
  if (/^[A-Za-z0-9@._+-]+$/.test(value)) return value;
  return `'${value.replaceAll("'", `'"'"'`)}'`;
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

function CredentialDetectionState({ hostName }: { hostName: string }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-muted/40 px-4 py-3">
      <Loader2Icon aria-label="Checking credentials" className="size-4 shrink-0 animate-spin" />
      <p className="text-sm text-muted-foreground">
        Checking which credentials are available on {hostName}…
      </p>
    </div>
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
    mode === "switch"
      ? `Signing out on ${hostName}`
      : mode === "update"
        ? `Installing the latest version on ${hostName}`
        : `Installing ${harnessLabel} on ${hostName}`;
  return (
    <>
      <DialogDescription>
        {mode === "switch"
          ? `Signing out of ${harnessLabel} on ${hostName} before starting a new sign-in.`
          : mode === "update"
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
        {harnessAccountKey(harness) === "claude"
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
  deviceLoginActive: boolean,
  harnessLabel: string,
  hostName: string,
): string {
  if (phase === "confirm") return `Switch ${harnessLabel} account?`;
  if (deviceLoginActive || phase === "device-error") {
    return `Sign in to ${harnessLabel} on ${hostName}`;
  }
  if (phase === "personal") return `Sign in to ${harnessLabel} on ${hostName}`;
  if (phase === "isaac") return `Set up Isaac for ${harnessLabel} on ${hostName}`;
  if (phase === "setup") return `Set up ${harnessLabel} on ${hostName}`;
  if (phase === "error") {
    if (mode === "update") return `Couldn’t update ${harnessLabel}`;
    if (mode === "install") return `Couldn’t install ${harnessLabel}`;
    return `Couldn’t reconfigure ${harnessLabel}`;
  }
  if (phase === "progress") {
    if (mode === "switch") return `Switching ${harnessLabel} account…`;
    if (mode === "update") return `Updating ${harnessLabel}…`;
    if (mode === "install") return `Installing ${harnessLabel}…`;
    return `Reconfiguring ${harnessLabel}…`;
  }
  return mode === "install" ? `Install ${harnessLabel}` : `Reconfigure ${harnessLabel}`;
}
