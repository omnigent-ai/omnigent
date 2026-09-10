// Onboarding step 2: pick where sessions are stored — Local or Cloud. The
// selected card expands to show its bullets. Local → Begin (start the local
// server); Cloud → Server setup (opens the deploy docs in the browser).

import { useRef, useState, type ComponentType, type ReactNode } from "react";
// Colored (brand) harness glyphs for the hero row — the Color subpath keeps
// antd out of the bundle, same rationale as the Mono icons in components/icons.
// Cursor has no Color variant (its logo is monochrome), so it uses Mono.
import ClaudeCodeColor from "@lobehub/icons/es/ClaudeCode/components/Color";
import CodexColor from "@lobehub/icons/es/Codex/components/Color";
import CursorMono from "@lobehub/icons/es/Cursor/components/Mono";
import {
  CheckCheck,
  GalleryHorizontalEnd,
  Import,
  Laptop,
  type LucideProps,
  TabletSmartphone,
  Terminal,
  Users,
  CloudIcon,
  TabletSmartphoneIcon,
  UsersIcon,
  PlayIcon,
  ExternalLinkIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type Mode = "local" | "cloud";

interface Detail {
  label: string;
  icon: ComponentType<LucideProps>;
}

const LOCAL_ICONS: { key: string; node: ReactNode; accent?: boolean }[] = [
  { key: "local", node: <Laptop className="size-8" />, accent: true },
  { key: "claude", node: <ClaudeCodeColor size={32} /> },
  { key: "codex", node: <CodexColor size={32} /> },
  { key: "cursor", node: <CursorMono size={32} /> },
];

const CLOUD_ICONS: { key: string; node: ReactNode; accent?: boolean }[] = [
  { key: "cloud", node: <CloudIcon className="size-8" />, accent: true },
  { key: "tablet", node: <TabletSmartphoneIcon className="size-8" />, accent: true },
  { key: "users", node: <UsersIcon className="size-8" />, accent: true },
  { key: "play", node: <PlayIcon className="size-8" />, accent: true },
];

const MODES: {
  id: Mode;
  title: string;
  details: Detail[];
  iconSet?: {
    key: string;
    node: ReactNode;
    accent?: boolean;
  }[];
}[] = [
  {
    id: "local",
    title: "Local",
    details: [
      {
        label: "Use Claude Code, Codex, Cursor, and other local harnesses from one UI.",
        icon: Terminal,
      },
      {
        label: "Keep session history and context persistent across every harness.",
        icon: GalleryHorizontalEnd,
      },
      { label: "Import existing chats from harnesses.", icon: Import },
    ],
    iconSet: LOCAL_ICONS,
  },
  {
    id: "cloud",
    title: "Cloud",
    details: [
      { label: "Everything included with Local, plus:", icon: CheckCheck },
      { label: "Access agents from any device", icon: TabletSmartphone },
      { label: "Co-drive live sessions with teammates", icon: Users },
    ],
    iconSet: CLOUD_ICONS,
  },
];

export function ModeSelectStep({
  onBack,
  onBegin,
  onCloudSetup,
}: {
  onBack: () => void;
  onBegin: () => void;
  onCloudSetup: () => void;
}) {
  const [mode, setMode] = useState<Mode>("local");
  const refs = useRef(new Map<Mode, HTMLButtonElement | null>());

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <h1 className="mb-3 pt-1 text-center text-base text-foreground">
        Where do you want your sessions to be stored?
      </h1>

      <div role="radiogroup" aria-label="Deployment mode" className="flex flex-1 flex-col gap-2">
        {MODES.map((m, index) => {
          const selected = m.id === mode;
          return (
            <>
              <button
                key={m.id}
                ref={(el) => {
                  refs.current.set(m.id, el);
                }}
                type="button"
                role="radio"
                aria-checked={selected}
                tabIndex={selected ? 0 : -1}
                onClick={() => setMode(m.id)}
                onKeyDown={(event) => {
                  const fwd = event.key === "ArrowDown" || event.key === "ArrowRight";
                  const back = event.key === "ArrowUp" || event.key === "ArrowLeft";
                  if (!fwd && !back) return;
                  event.preventDefault();
                  const next = MODES[(index + (fwd ? 1 : -1) + MODES.length) % MODES.length].id;
                  setMode(next);
                  refs.current.get(next)?.focus();
                }}
                className={cn(
                  "flex flex-col rounded-lg cursor-pointer px-3 py-2.5 text-left transition-[border-color,background-color]",
                  selected ? "" : "hover:bg-muted",
                )}
              >
                <div
                  className={cn(
                    "flex justify-center gap-1.5 transition-all duration-300",
                    selected ? "max-h-32 mb-4" : "max-h-0 overflow-hidden mb-0",
                  )}
                  aria-hidden="true"
                >
                  {m.iconSet?.map(({ key, node, accent }) => (
                    <span
                      key={key}
                      className={cn(
                        "-mr-4 flex size-14 items-center justify-center rounded-2xl border bg-background",
                        accent ? "border-brand-accent/25 text-brand-accent" : "border-border",
                      )}
                    >
                      {node}
                    </span>
                  ))}
                </div>
                <span className="flex items-center gap-2">
                  <span
                    aria-hidden
                    className={cn(
                      "flex size-4 items-center justify-center rounded-full border",
                      selected ? "border-primary" : "border-border",
                    )}
                  >
                    {selected && <span className="size-2 rounded-full bg-primary" />}
                  </span>
                  <span className="text-[14px] font-medium text-foreground">{m.title}</span>
                </span>

                {/* Expanding details: grid-rows trick animates open/closed with no lib. */}
                <span
                  className={cn(
                    "grid transition-[grid-template-rows] duration-300",
                    selected ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
                  )}
                >
                  <span className="overflow-hidden">
                    <span className="mt-2 flex flex-col gap-1.5">
                      {m.details.map((detail) => (
                        <span
                          key={detail.label}
                          className="flex gap-2 text-base text-muted-foreground"
                        >
                          <detail.icon className="mt-0.5 size-4 shrink-0" aria-hidden />
                          <span>{detail.label}</span>
                        </span>
                      ))}
                    </span>
                  </span>
                </span>
              </button>
              {index === 0 && <div className="h-2 border-b-1 border-border" />}
            </>
          );
        })}
      </div>

      <div className="mt-3 flex gap-2">
        <Button variant="outline" className="flex-1" onClick={onBack}>
          Back
        </Button>
        {mode === "local" ? (
          <Button className="flex-1" onClick={onBegin}>
            Begin
          </Button>
        ) : (
          <Button className="flex-1" onClick={onCloudSetup}>
            Server setup <ExternalLinkIcon className="size-4" />
          </Button>
        )}
      </div>
    </div>
  );
}
