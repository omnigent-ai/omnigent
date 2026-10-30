import { useEffect, useState } from "react";
import {
  ArrowDownIcon,
  CheckIcon,
  Clock3Icon,
  GitPullRequestIcon,
  MessageSquareIcon,
  Repeat2Icon,
  SearchIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled, type ServerInfo } from "@/lib/capabilities";
import { withBasePath } from "@/lib/basePath";
import { useAppName } from "@/lib/branding";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";

interface Step {
  id: "schedule" | "watch" | "context" | "configure";
  title: string;
  description: string;
  caption: string;
  result?: string;
  icon: LucideIcon;
}

/** Mounted on the Automations page only after its first successful load. */
export function AutomationsIntro({ onClose }: { onClose: () => void }) {
  const info = useServerInfo();
  const viewerId = useViewerId();
  const rebasePath = useRebasePath();
  if (info === "loading" || viewerId === null) return null;
  const server = getOmnigentServerIdentity();
  const mountPath = withBasePath(rebasePath("/"));
  const storageKey =
    server === null
      ? null
      : `omnigent:automations-intro:v1:${JSON.stringify([server, mountPath, viewerId])}`;
  return (
    <IntroDialog
      key={storageKey ?? JSON.stringify([getOmnigentHostGeneration(), mountPath, viewerId])}
      storageKey={storageKey}
      info={info}
      onClose={onClose}
    />
  );
}

function IntroDialog({
  storageKey,
  info,
  onClose,
}: {
  storageKey: string | null;
  info: ServerInfo;
  onClose: () => void;
}) {
  const appName = useAppName();
  const [open, setOpen] = useState(() => {
    if (storageKey === null) return true;
    try {
      return localStorage.getItem(storageKey) !== "seen";
    } catch {
      return true;
    }
  });
  const [stepIndex, setStepIndex] = useState(0);
  useEffect(() => {
    if (storageKey === null) return;
    function onStorage(event: StorageEvent) {
      if (event.key === storageKey && event.newValue === "seen") setOpen(false);
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [storageKey]);

  function dismiss() {
    setOpen(false);
    if (storageKey === null) return;
    try {
      localStorage.setItem(storageKey, "seen");
    } catch {
      // Storage may be disabled; closing the guide must still work.
    }
  }

  const steps: Step[] = [
    {
      id: "schedule",
      title: "Run on a schedule",
      description:
        "Set a recurring time for your agent to investigate issues, review code, or prepare a report.",
      caption: "Every weekday · 9:00 AM",
      result: "Your results, ready to review",
      icon: Clock3Icon,
    },
  ];
  if (info.scheduled_task_session_modes && isFeatureEnabled(info, "session_orchestration")) {
    steps.push({
      id: "watch",
      title: "Watch for new work",
      description:
        "Use Jira, Slack, or GitHub templates to check periodically and start sessions for matching items. Your agent needs access to the service.",
      caption: "Jira · Slack · GitHub",
      result: "Start work on matching items",
      icon: SearchIcon,
    });
  }
  if (info.scheduled_task_session_modes) {
    steps.push({
      id: "context",
      title: "Keep context between runs",
      description:
        "Start fresh each run, or continue one session so your agent can build on its earlier work.",
      caption: "Continue one session",
      result: "Build on what you learned",
      icon: Repeat2Icon,
    });
  }
  steps.push({
    id: "configure",
    title: "Make it yours",
    description: info.managed_sandboxes_enabled
      ? "Choose your agent and a connected computer or available sandbox. Customize model and permission settings where supported."
      : "Choose your agent and connected computer. Keep the server and computer running, and customize model and permission settings where supported.",
    caption: "Your agent · Your instructions",
    icon: SlidersHorizontalIcon,
  });
  const activeIndex = Math.min(stepIndex, steps.length - 1);
  const activeStep = steps[activeIndex];

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) dismiss();
      }}
    >
      <DialogContent
        className="max-h-[90dvh] gap-0 overflow-y-auto p-0 sm:max-w-5xl"
        onPointerDownOutside={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          onClose();
        }}
      >
        <div className="grid md:grid-cols-[1.05fr_1fr]">
          <div className="flex flex-col p-6 pt-12 md:p-8 md:pt-10">
            <DialogTitle className="text-2xl font-semibold tracking-tight">
              Introducing Automations
            </DialogTitle>
            <DialogDescription className="mt-3 text-sm leading-relaxed">
              Put recurring work on autopilot with {appName}.
            </DialogDescription>
            <ol aria-label="Automation features" className="my-6 space-y-2">
              {steps.map((step, index) => (
                <li key={step.id}>
                  <button
                    type="button"
                    aria-current={index === activeIndex ? "step" : undefined}
                    onClick={() => setStepIndex(index)}
                    className={cn(
                      "flex w-full gap-3 rounded-2xl p-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-ring",
                      index === activeIndex ? "bg-primary/10" : "hover:bg-muted/60",
                    )}
                  >
                    <span
                      className={cn(
                        "flex size-7 shrink-0 items-center justify-center rounded-full border text-xs",
                        index === activeIndex
                          ? "border-primary/20 bg-background text-primary"
                          : "text-muted-foreground",
                      )}
                    >
                      {index + 1}
                    </span>
                    <span>
                      <span className="block text-sm font-medium">{step.title}</span>
                      <span
                        className={cn(
                          "mt-1 text-sm leading-relaxed text-muted-foreground md:block",
                          index === activeIndex ? "block" : "hidden",
                        )}
                      >
                        {step.description}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ol>
            <div className="mt-auto flex items-center justify-end gap-2">
              <Button variant="ghost" onClick={dismiss} componentId="tasks.intro.dismiss">
                Maybe later
              </Button>
              <Button
                onClick={() =>
                  activeIndex === steps.length - 1 ? dismiss() : setStepIndex(activeIndex + 1)
                }
                componentId="tasks.intro.next"
              >
                {activeIndex === steps.length - 1 ? "Done" : "Next"}
              </Button>
            </div>
          </div>
          <div className="relative flex flex-col items-center justify-center border-t bg-muted/40 p-6 md:min-h-[560px] md:border-t-0 md:border-l md:p-10">
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-0 rounded-br-[30px] bg-[radial-gradient(ellipse_at_center,var(--color-primary),transparent_70%)] opacity-[0.07]"
            />
            <div
              className="relative hidden w-full flex-col items-center md:flex"
              aria-hidden="true"
            >
              <StepIllustration
                step={activeStep}
                appName={appName}
                sandbox={info.managed_sandboxes_enabled}
              />
            </div>
            <div className="relative md:hidden" aria-hidden="true">
              <IllustrationCard icon={activeStep.icon}>{activeStep.caption}</IllustrationCard>
            </div>
            <p
              aria-live="polite"
              className="mt-4 text-xs text-muted-foreground md:absolute md:bottom-7"
            >
              {activeIndex + 1} of {steps.length} · {activeStep.title}
            </p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function IllustrationCard({
  icon: Icon,
  children,
}: {
  icon: LucideIcon;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 rounded-2xl border bg-background px-5 py-4 text-sm font-medium shadow-sm">
      <Icon className="size-5 shrink-0 text-primary" />
      {children}
    </div>
  );
}

function StepIllustration({
  step,
  appName,
  sandbox,
}: {
  step: Step;
  appName: string;
  sandbox: boolean;
}) {
  if (step.id === "configure") {
    return (
      <div className="w-full max-w-72 rounded-2xl border bg-background p-5 shadow-sm">
        <div className="mb-5 flex items-center gap-2 font-medium">
          <step.icon className="size-5 text-primary" />
          Your automation
        </div>
        {[
          "Your agent",
          sandbox ? "Computer or sandbox" : "Connected computer",
          "Your instructions",
        ].map((label) => (
          <div
            key={label}
            className="flex items-center justify-between border-t py-3 text-sm text-muted-foreground"
          >
            {label}
            <CheckIcon className="size-4 text-primary" />
          </div>
        ))}
      </div>
    );
  }
  return (
    <>
      <IllustrationCard icon={step.icon}>
        {step.id === "context" ? "Earlier work" : step.caption}
      </IllustrationCard>
      <ArrowDownIcon className="my-3 size-5 text-muted-foreground/50" />
      <IllustrationCard icon={SparklesIcon}>
        {step.id === "context" ? step.caption : appName}
      </IllustrationCard>
      <ArrowDownIcon className="my-3 size-5 text-muted-foreground/50" />
      <IllustrationCard icon={step.id === "watch" ? GitPullRequestIcon : MessageSquareIcon}>
        {step.result}
      </IllustrationCard>
    </>
  );
}
