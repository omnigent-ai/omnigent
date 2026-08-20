/**
 * "Set up <agent> on <host>" dialog for the New Chat landing screen.
 *
 * Renders the harness's setup requirements as a checklist, mirroring what
 * ``omni setup`` walks a user through. The step descriptors are authored by
 * the server (``/v1/harnesses`` → ``setup_steps``); this component marks each
 * done/todo from the host's readiness and offers the right control per step:
 *   - a one-click Install for a server-installable harness,
 *   - an inline "Add a credential" form (adopt an existing key / paste an API
 *     key / configure a gateway) for the Claude / Codex / Pi families the UI can
 *     authenticate,
 *   - a click-to-copy command for a step the UI can't do yet.
 * When no step is UI-actionable the dialog collapses to a single "run omnigent
 * setup" signpost. There is no progress counter — the per-row dots already show
 * each step's state.
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
import { useHosts, useInstallHarness, useInstallingHarnesses, type Host } from "@/hooks/useHosts";
import {
  harnessAuthableOnHost,
  harnessInstallableOnHost,
  resolveSetupSteps,
  type ResolvedSetupStep,
} from "@/lib/harnessSetup";
import { HarnessCredentialForm } from "@/shell/HarnessCredentialForm";

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
  // snapshot passed at open time: a successful install / credential write
  // patches the ["hosts"] cache, and the dialog must reflect that (flip ✓)
  // without being closed and reopened. Falls back to the snapshot while the
  // query is loading.
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const host = hosts?.find((h) => h.host_id === hostProp?.host_id) ?? hostProp;
  const install = useInstallHarness(host?.host_id ?? "");
  const steps = resolveSetupSteps(
    harness ? setupStepsByHarness[harness] : undefined,
    harness,
    host,
  );
  // Defence in depth: only render the one-click Install when the server's
  // allowlist accepts this harness (guards against step-catalog/allowlist
  // drift). Likewise the inline credential form only for a harness whose
  // credential the UI can actually write.
  const installable = harnessInstallableOnHost(info, harness, host);
  const authable = harnessAuthableOnHost(info, harness, host);
  const name = agentName ?? harness ?? "this agent";

  // Auth is a SECOND step: gate the credential form until the binary is
  // present, so the checklist runs Install → Set up auth in order. Without this,
  // a not-yet-installed harness shows both buttons and an auth-first write
  // would succeed yet leave the dot red (readiness is binary-missing until the
  // CLI lands) — a contradictory "saved but not ready" state. "Installed" =
  // no install step is still to-do.
  const installComplete = steps.every((s) => s.kind !== "install" || s.status === "done");

  // A step is UI-actionable when we can perform it here: an install we're
  // allowed to run, or an auth step for a harness we can authenticate. When NO
  // step is actionable (a curl/own-login harness), collapse to one signpost.
  const anyActionable = steps.some(
    (s) =>
      (s.action === "install" && installable) ||
      (s.kind === "auth" && authable) ||
      s.command != null,
  );
  const allDone = steps.length > 0 && steps.every((s) => s.status === "done");

  // Which harness ids have an install in flight, read from React Query's
  // mutation state (not a local set). The dialog is one persistent instance
  // sharing a single install mutation observer, and that observer only
  // remembers the LATEST mutate() call's per-call callbacks. Tracking in-flight
  // installs via mutate()'s onSettled therefore lost an earlier harness the
  // moment a second install started — leaving the first stuck on "Installing…".
  // useInstallingHarnesses derives the set from mutation state, so every row
  // reflects its OWN harness's state regardless of firing order.
  const installing = useInstallingHarnesses(host?.host_id ?? "");
  const installingThisHarness = !!harness && installing.has(harness);

  const startInstall = (h: string) => {
    // Only the toast rides on the per-call callbacks; the in-flight indicator
    // (mutation state) and the readiness cache patch (config-level onSuccess in
    // useInstallHarness) are both observer-independent, so a concurrent second
    // install can't strand this one. A lost toast is cosmetic; a stuck spinner
    // or an unpatched badge is the bug we're avoiding.
    install.mutate(h, {
      onSuccess: (result) => {
        // The install succeeded, but the harness may still need a credential
        // (e.g. Codex → "needs-auth"): key the toast on the refreshed readiness
        // so it doesn't claim "ready" while a sign-in row is still showing.
        const ready = result.configured_harnesses[h] === true;
        showToast(
          ready
            ? `${name} is ready on ${host?.name}.`
            : `${name} installed on ${host?.name} — one more step to finish setup.`,
        );
      },
      onError: (err) => showToast(`Couldn't install ${name}: ${err.message}`, { duration: 0 }),
    });
  };

  const hasSteps = steps.length > 0;
  const collapsed = !hasSteps || !anyActionable;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg" data-testid="harness-setup-dialog">
        <DialogHeader>
          <DialogTitle>
            Set up {name} on {host?.name}
          </DialogTitle>
          {/* Only show a description when it adds something the title doesn't:
              the done / needs-more-setup states. The generic "complete these
              steps…" just echoes the title, so it's omitted for the common
              checklist case. */}
          {allDone ? (
            <DialogDescription>{`${name} is ready on ${host?.name}.`}</DialogDescription>
          ) : collapsed ? (
            <DialogDescription>{`${name} needs a bit more setup on ${host?.name}.`}</DialogDescription>
          ) : null}
        </DialogHeader>

        {collapsed && !allDone ? (
          // No step the UI can perform (curl-install / own-login harness, or no
          // published steps) and not yet done. One clear signpost — never a
          // dead-end. When everything IS done we fall through to the checklist
          // (all green ticks) rather than showing a "run omni setup"
          // signpost that would contradict the "is ready" description.
          <p className="py-1 text-ui text-muted-foreground" data-testid="harness-setup-empty">
            Run <code className="rounded bg-muted px-1 py-0.5 font-mono text-sm">omni setup</code>{" "}
            on {host?.name} to finish setting up {name}.
          </p>
        ) : hasSteps ? (
          <ul className="flex flex-col gap-3 py-1">
            {steps.map((step) => (
              <SetupStepRow
                key={step.kind}
                step={step}
                label={name}
                installable={installable}
                authable={authable}
                installComplete={installComplete}
                installing={installingThisHarness}
                host={host}
                onInstall={startInstall}
              />
            ))}
          </ul>
        ) : null}

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
  label,
  installable,
  authable,
  installComplete,
  installing,
  host,
  onInstall,
}: {
  step: ResolvedSetupStep;
  /** Friendly agent/harness name for the credential form's toasts. */
  label: string;
  installable: boolean;
  authable: boolean;
  installComplete: boolean;
  installing: boolean;
  host: Host | undefined | null;
  onInstall: (harness: string) => void;
}) {
  // An auth step for a harness the UI can authenticate expands an inline
  // credential form — but only once the binary is installed, so the checklist
  // runs Install → Set up auth in order (readiness can't be "has key, no
  // binary", so an auth-first write would leave the dot red).
  const isAuthStep = step.kind === "auth" && authable && step.status !== "done";
  const isAuthForm = isAuthStep && installComplete;
  // Auth is unlocked but blocked on install: show a disabled "Set up auth" so the
  // step reads as a real next action rather than vanishing.
  const authPending = isAuthStep && !installComplete;
  const [formOpen, setFormOpen] = useState(false);

  const done = step.status === "done";
  // The server's detail is future-tense ("We'll install …") — right for a
  // pending step, wrong under a green check. Show a past-tense confirmation
  // once done instead of the stale promise.
  const detail = done ? doneDetail(step.kind) : step.detail;

  return (
    <li className="flex flex-col gap-2" data-testid={`harness-setup-step-${step.kind}`}>
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5">
          <StepIcon status={step.status} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-ui font-medium">{step.title}</span>
          {/* While this harness's install is in flight, the detail line becomes
              the progress caption — kept next to the install step it describes
              rather than stranded at the bottom of the dialog. */}
          {step.kind === "install" && installing ? (
            <span className="text-sm text-muted-foreground" data-testid="harness-setup-installing">
              Installing on {host?.name} — this can take a few minutes for larger agents.
            </span>
          ) : (
            detail && <span className="text-sm text-muted-foreground">{detail}</span>
          )}
        </div>
        {/* Control: one-click Install (when the allowlist accepts it), an
            "Add credential" toggle (auth step the UI can do), else the command
            to copy. */}
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
        ) : isAuthForm ? (
          <Button
            type="button"
            size="sm"
            variant={formOpen ? "outline" : "default"}
            data-testid="harness-setup-add-credential"
            aria-expanded={formOpen}
            onClick={() => setFormOpen((v) => !v)}
          >
            {formOpen ? "Cancel" : "Set up auth"}
          </Button>
        ) : authPending ? (
          // Installed first: the credential form unlocks once the binary lands.
          <Button
            type="button"
            size="sm"
            disabled
            data-testid="harness-setup-add-credential"
            title="Install first, then set up authentication"
          >
            Set up auth
          </Button>
        ) : step.command ? (
          <CopyCommand command={step.command} />
        ) : null}
      </div>

      {isAuthForm && formOpen && (
        <div className="ml-7">
          <HarnessCredentialForm
            harness={step.harness}
            label={label}
            host={host}
            command={step.command}
            onDone={() => setFormOpen(false)}
          />
        </div>
      )}
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
      className="group flex shrink-0 items-center gap-1.5 rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
