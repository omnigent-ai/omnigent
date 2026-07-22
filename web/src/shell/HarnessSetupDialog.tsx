/**
 * "Set up <agent> on <host>" dialog for the New Chat landing screen.
 *
 * Renders the harness's setup requirements as an always-visible checklist with
 * a "N of M done" progress header, mirroring what ``omnigent setup`` walks a
 * user through. The steps are authored by the server (``/v1/harnesses`` →
 * ``setup_steps``); this component marks each done/todo from the host's
 * readiness and offers the right control per step: a one-click Install for
 * server-performed steps, or a click-to-copy command the user runs on the host.
 */

import { useState } from "react";
import { CheckIcon, CircleCheckIcon, CircleDashedIcon, CopyIcon, InfoIcon } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { useHarnessSetupSteps } from "@/lib/agentLabels";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useHosts, useInstallHarness, type Host } from "@/hooks/useHosts";
import {
  harnessInstallableOnHost,
  resolveSetupSteps,
  setupProgress,
  type ResolvedSetupStep,
} from "@/lib/harnessSetup";

export function HarnessSetupDialog({
  open,
  onOpenChange,
  agentName,
  harness,
  host: hostProp,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agentName: string | undefined;
  harness: string | null;
  host: Host | undefined | null;
}) {
  const setupStepsByHarness = useHarnessSetupSteps();
  const info = useServerInfo();
  // Re-resolve the host LIVE from the hosts query rather than trusting the
  // snapshot passed at open time: a successful install patches the ["hosts"]
  // cache, and the dialog must reflect that (flip ✓, recompute progress)
  // without being closed and reopened. Falls back to the snapshot while the
  // query is loading.
  const { data: hosts } = useHosts();
  const host = hosts?.find((h) => h.host_id === hostProp?.host_id) ?? hostProp;
  const install = useInstallHarness(host?.host_id ?? "");
  const steps = resolveSetupSteps(
    harness ? setupStepsByHarness[harness] : undefined,
    harness,
    host,
  );
  // Defence in depth: only render the one-click Install when the server's
  // allowlist actually accepts this harness on this host. A server step with
  // action "install" is the primary signal, but gating on the allowlist too
  // means the UI can never offer an install the route would reject (409/400)
  // if the two ever drift.
  const installable = harnessInstallableOnHost(info, harness, host);
  const { done, total } = setupProgress(steps);
  const name = agentName ?? harness ?? "this agent";
  const allDone = total > 0 && done === total;
  // Track which harness ids have an install in flight. The dialog is one
  // persistent instance sharing a single install mutation, so its isPending
  // only reflects the latest call — useless when installs run concurrently or
  // the dialog switches harnesses mid-install. Each mutate() call adds its
  // harness on start and removes it on settle (per-call callbacks fire
  // independently), so every row reflects its OWN harness's real state.
  const [installing, setInstalling] = useState<ReadonlySet<string>>(new Set());
  const installingThisHarness = !!harness && installing.has(harness);

  const startInstall = (h: string) => {
    setInstalling((prev) => new Set(prev).add(h));
    install.mutate(h, {
      onSuccess: (result) => {
        // The install succeeded, but the harness may still need a credential
        // (e.g. Codex → "needs-auth"): the checklist shows the remaining
        // sign-in row, so a flat "is ready" toast would contradict it. Key the
        // toast on the refreshed readiness the install returned — "ready" only
        // when the harness is actually launchable, otherwise "installed" with a
        // nudge to the remaining step.
        const ready = result.configured_harnesses[h] === true;
        showToast(
          ready
            ? `${name} is ready on ${host?.name}.`
            : `${name} installed on ${host?.name} — one more step to finish setup.`,
        );
      },
      onError: (err) => showToast(`Couldn't install ${name}: ${err.message}`, { duration: 0 }),
      onSettled: () =>
        setInstalling((prev) => {
          const next = new Set(prev);
          next.delete(h);
          return next;
        }),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg" data-testid="harness-setup-dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span>
              Set up {name} on {host?.name}
            </span>
            {total > 0 && (
              <span
                className="rounded-full bg-muted px-2 py-0.5 text-xs font-normal text-muted-foreground"
                data-testid="harness-setup-progress"
              >
                {done} of {total} done
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            {allDone
              ? `${name} is ready on ${host?.name}.`
              : steps.length > 0
                ? `Complete these steps on ${host?.name} to use ${name}.`
                : `${name} needs a bit more setup on ${host?.name}.`}
          </DialogDescription>
        </DialogHeader>

        {steps.length > 0 ? (
          <ul className="flex flex-col gap-3 py-1">
            {steps.map((step) => (
              <SetupStepRow
                key={step.kind}
                step={step}
                installable={installable}
                installing={installingThisHarness}
                onInstall={startInstall}
              />
            ))}
          </ul>
        ) : (
          // The server published no steps for this spelling (a harness the UI
          // can't yet guide). Don't leave an empty dialog — point at the CLI.
          <p className="py-1 text-sm text-muted-foreground" data-testid="harness-setup-empty">
            Run{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">omnigent setup</code>{" "}
            on {host?.name} to finish setting up {name}.
          </p>
        )}
        {installingThisHarness && (
          <p className="text-xs text-muted-foreground" data-testid="harness-setup-installing">
            Installing on {host?.name} — this can take a few minutes for larger agents.
          </p>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            {allDone ? "Done" : "Close"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Past-tense confirmation shown under a completed step, replacing the
 *  server's future-tense "we'll do X" detail once X is done. */
function doneDetail(kind: string): string {
  if (kind === "install") return "Installed on the host.";
  if (kind === "auth") return "Signed in on the host.";
  return "Done.";
}

function StepIcon({ status }: { status: ResolvedSetupStep["status"] }) {
  if (status === "done") {
    return <CircleCheckIcon className="size-4 shrink-0 text-emerald-600 dark:text-emerald-500" />;
  }
  if (status === "unknown") {
    return <InfoIcon className="size-4 shrink-0 text-muted-foreground" />;
  }
  return <CircleDashedIcon className="size-4 shrink-0 text-amber-600 dark:text-amber-500" />;
}

function SetupStepRow({
  step,
  installable,
  installing,
  onInstall,
}: {
  step: ResolvedSetupStep;
  installable: boolean;
  installing: boolean;
  onInstall: (harness: string) => void;
}) {
  const done = step.status === "done";
  // The server's detail is future-tense ("We'll install …") — right for a
  // pending step, wrong under a green check. Show a past-tense confirmation
  // once done instead of the stale promise.
  const detail = done ? doneDetail(step.kind) : step.detail;
  return (
    <li className="flex items-start gap-2.5" data-testid={`harness-setup-step-${step.kind}`}>
      <span className="mt-0.5">
        <StepIcon status={step.status} />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-sm font-medium">{step.title}</span>
        {detail && <span className="text-xs text-muted-foreground">{detail}</span>}
      </div>
      {/* No control once the step is done. Otherwise: a one-click Install for a
          server-performed step the allowlist still accepts (`installable`
          guards against drift between the step catalog and the install route),
          else the command to run on the host. */}
      {done ? null : step.action === "install" && installable ? (
        <Button
          type="button"
          size="sm"
          loading={installing}
          data-testid="harness-setup-install"
          onClick={() => onInstall(step.harness)}
        >
          Install
        </Button>
      ) : step.command ? (
        <CopyCommand command={step.command} />
      ) : null}
    </li>
  );
}

/**
 * The command a user runs on the host for a step we can't perform for them
 * (login, an API-key/gateway setup). Click-to-copy so it isn't a dead-end
 * copy-by-hand instruction; a check mark confirms the copy.
 */
function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      data-testid="harness-setup-command"
      title="Copy command"
      className="group flex shrink-0 items-center gap-1.5 rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onClick={() => {
        void copyText(command)
          .then(() => {
            setCopied(true);
            showToast("Copied to clipboard.");
          })
          .catch(() => showToast("Couldn't copy — select and copy manually.", { duration: 0 }));
      }}
    >
      <span>{command}</span>
      {copied ? (
        <CheckIcon className="size-3 shrink-0 text-emerald-600 dark:text-emerald-500" />
      ) : (
        <CopyIcon className="size-3 shrink-0 opacity-60 group-hover:opacity-100" />
      )}
    </button>
  );
}
