// Post-setup "Your imports are ready" modal: one tab per harness, showing the
// credential Omnigent adopted (read-only) and the MCP servers, skills, and
// plugins found there as opt-in checkboxes, all selected by default.

import { useId, useState, type ReactNode } from "react";
import { ArrowRight, Check, XIcon } from "lucide-react";
import omnigentLogo from "@/assets/omnigent-starfish-icon.png";
import BlobGraphic from "@/components/onboarding/BlobGraphic";
import {
  BRAND_HARNESSES,
  type BrandHarness,
  HarnessBrandIcon,
  HarnessIconTile,
  harnessDisplayName,
} from "@/components/onboarding/harnessBrand";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

export type ImportHarness = BrandHarness;

export interface ImportCredential {
  harness: ImportHarness;
  /** Where the harness's login comes from, e.g. "Databricks AI Gateway". */
  source: string;
}

export interface ImportMcpServer {
  id: string;
  name: string;
  harness: ImportHarness;
  toolCount?: number;
}

export interface ImportSkill {
  id: string;
  name: string;
  harness: ImportHarness;
}

export interface ImportPlugin {
  id: string;
  name: string;
  harness: ImportHarness;
  skillCount?: number;
}

export interface ImportContext {
  credentials: ImportCredential[];
  mcps: ImportMcpServer[];
  skills: ImportSkill[];
  plugins: ImportPlugin[];
}

/** Ids of the MCP servers, skills, and plugins left checked on Confirm. */
export interface ImportSelection {
  mcps: string[];
  skills: string[];
  plugins: string[];
}

/** Harness icons → Omnigent starfish, over the onboarding blob graphic. */
function ImportBand() {
  return (
    <div className="relative h-[200px] max-h-[25vh] shrink-0 overflow-hidden">
      <BlobGraphic />
      <div className="absolute inset-0 flex items-center justify-center gap-5" aria-hidden="true">
        <div className="flex -space-x-1">
          {BRAND_HARNESSES.map((harness) => (
            <HarnessIconTile key={harness}>
              <HarnessBrandIcon harness={harness} size={32} />
            </HarnessIconTile>
          ))}
        </div>
        <ArrowRight className="size-4 text-muted-foreground" />
        <HarnessIconTile>
          <img src={omnigentLogo} alt="" className="size-8 object-contain" />
        </HarnessIconTile>
      </div>
    </div>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-xs text-muted-foreground">{children}</p>;
}

/** Shared row chrome so credential and checkbox rows keep one divider/gap contract. */
function ImportRow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <li className={cn("flex items-center gap-3 border-b border-border last:border-b-0", className)}>
      {children}
    </li>
  );
}

function CredentialRow({ credential }: { credential: ImportCredential }) {
  const { harness, source } = credential;
  return (
    <ul>
      <ImportRow className="py-3">
        <span
          className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted"
          aria-hidden="true"
        >
          <HarnessBrandIcon harness={harness} size={16} />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-ui font-medium text-foreground">
            {harnessDisplayName(harness)}
          </span>
          <span className="truncate text-xs text-muted-foreground">{source}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
          <Check className="size-3.5 text-success" aria-hidden="true" />
          Imported
        </span>
      </ImportRow>
    </ul>
  );
}

interface SelectableRow {
  id: string;
  name: string;
  metadata?: string;
}

/** A labelled MCPs / Skills / Plugins list within a harness tab; hidden when empty. */
function AssetGroup({
  label,
  componentId,
  rows,
  selected,
  onToggle,
}: {
  label: string;
  componentId: string;
  rows: SelectableRow[];
  selected: ReadonlySet<string>;
  onToggle: (id: string, checked: boolean) => void;
}) {
  const idPrefix = useId();
  if (rows.length === 0) return null;
  const headingId = `${idPrefix}-heading`;
  return (
    <section aria-labelledby={headingId} className="pt-3">
      <h3 id={headingId} className="pb-1 text-xs font-medium text-muted-foreground">
        {label} <span className="text-muted-foreground/70">{rows.length}</span>
      </h3>
      <ul>
        {rows.map(({ id, name, metadata }, index) => {
          const inputId = `${idPrefix}-${index}`;
          return (
            <ImportRow key={id} className="py-2">
              <Checkbox
                id={inputId}
                componentId={componentId}
                checked={selected.has(id)}
                onCheckedChange={(checked) => onToggle(id, checked === true)}
              />
              <label
                htmlFor={inputId}
                className="min-w-0 flex-1 cursor-pointer truncate text-ui font-medium text-foreground"
              >
                {name}
              </label>
              {metadata && (
                <span className="shrink-0 text-xs text-muted-foreground">{metadata}</span>
              )}
            </ImportRow>
          );
        })}
      </ul>
    </section>
  );
}

function countLabel(count: number | undefined, noun: string): string | undefined {
  if (count == null) return undefined;
  return `${count} ${count === 1 ? noun : `${noun}s`}`;
}

function toggle(set: ReadonlySet<string>, id: string, checked: boolean): Set<string> {
  const next = new Set(set);
  if (checked) next.add(id);
  else next.delete(id);
  return next;
}

/** Harnesses with a credential or any asset, in the shared brand order. */
function detectedHarnesses(context: ImportContext): ImportHarness[] {
  const found = new Set<ImportHarness>(
    [...context.credentials, ...context.mcps, ...context.skills, ...context.plugins].map(
      (item) => item.harness,
    ),
  );
  return BRAND_HARNESSES.filter((harness) => found.has(harness));
}

export interface ImportContextModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  context: ImportContext;
  onConfirm: (selection: ImportSelection) => void;
}

export function ImportContextModal({
  open,
  onOpenChange,
  context,
  onConfirm,
}: ImportContextModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex h-[640px] max-h-[85vh] flex-col gap-0 overflow-hidden rounded-[20px] p-0 sm:max-w-[560px]"
      >
        {/* Content unmounts on close, so the selection resets to all-checked
            each time the modal reopens. */}
        <ImportContextBody
          context={context}
          onConfirm={(selection) => {
            onConfirm(selection);
            onOpenChange(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function ImportContextBody({
  context,
  onConfirm,
}: Pick<ImportContextModalProps, "context" | "onConfirm">) {
  const allIds = (items: { id: string }[]) => new Set(items.map((item) => item.id));
  const [selectedMcps, setSelectedMcps] = useState<ReadonlySet<string>>(() => allIds(context.mcps));
  const [selectedSkills, setSelectedSkills] = useState<ReadonlySet<string>>(() =>
    allIds(context.skills),
  );
  const [selectedPlugins, setSelectedPlugins] = useState<ReadonlySet<string>>(() =>
    allIds(context.plugins),
  );
  const harnesses = detectedHarnesses(context);

  const confirm = () => {
    const kept = (items: { id: string }[], selected: ReadonlySet<string>) =>
      items.filter((item) => selected.has(item.id)).map((item) => item.id);
    onConfirm({
      mcps: kept(context.mcps, selectedMcps),
      skills: kept(context.skills, selectedSkills),
      plugins: kept(context.plugins, selectedPlugins),
    });
  };

  return (
    <>
      <ImportBand />
      <DialogClose asChild>
        <Button variant="ghost" size="icon-sm" className="absolute top-3 right-3 z-10">
          <XIcon className="size-4 text-foreground/70" />
          <span className="sr-only">Close</span>
        </Button>
      </DialogClose>

      {/* On short viewports the body scrolls so the Confirm footer stays reachable. */}
      <div className="no-scrollbar flex min-h-0 flex-1 flex-col overflow-y-auto px-5 pt-5">
        <div className="flex flex-col items-center gap-1 py-2 text-center">
          <DialogTitle className="min-h-0 pr-0 text-2xl leading-8 font-normal tracking-[-0.02em]">
            Your imports are ready
          </DialogTitle>
          <DialogDescription className="max-w-[480px] text-[14px] leading-5">
            Review what Omnigent brought over from your harnesses.
          </DialogDescription>
        </div>

        {harnesses.length === 0 ? (
          <EmptyState>Nothing to import from your harnesses</EmptyState>
        ) : (
          <Tabs
            defaultValue={harnesses[0]}
            componentId="onboarding.import.tabs"
            className="mt-5 min-h-36 flex-1 gap-0"
          >
            <TabsList
              variant="line"
              className="h-9 w-full justify-start gap-4 rounded-none border-b border-border p-0"
            >
              {harnesses.map((harness) => (
                <TabsTrigger key={harness} value={harness} className="flex-none gap-1.5 px-0">
                  <span aria-hidden="true" className="flex">
                    <HarnessBrandIcon harness={harness} size={14} />
                  </span>
                  {harnessDisplayName(harness)}
                </TabsTrigger>
              ))}
            </TabsList>
            <div className="no-scrollbar min-h-0 flex-1 overflow-y-auto pt-2">
              {harnesses.map((harness) => {
                const own = <T extends { harness: ImportHarness }>(items: T[]) =>
                  items.filter((item) => item.harness === harness);
                const credential = context.credentials.find((c) => c.harness === harness);
                const mcps = own(context.mcps);
                const skills = own(context.skills);
                const plugins = own(context.plugins);
                return (
                  <TabsContent key={harness} value={harness}>
                    {credential && <CredentialRow credential={credential} />}
                    <AssetGroup
                      label="MCPs"
                      componentId="onboarding.import.mcp"
                      rows={mcps.map((mcp) => ({
                        ...mcp,
                        metadata: countLabel(mcp.toolCount, "tool"),
                      }))}
                      selected={selectedMcps}
                      onToggle={(id, checked) => setSelectedMcps((s) => toggle(s, id, checked))}
                    />
                    <AssetGroup
                      label="Skills"
                      componentId="onboarding.import.skill"
                      rows={skills.map((skill) => ({ id: skill.id, name: `$${skill.name}` }))}
                      selected={selectedSkills}
                      onToggle={(id, checked) => setSelectedSkills((s) => toggle(s, id, checked))}
                    />
                    <AssetGroup
                      label="Plugins"
                      componentId="onboarding.import.plugin"
                      rows={plugins.map((plugin) => ({
                        ...plugin,
                        metadata: countLabel(plugin.skillCount, "skill"),
                      }))}
                      selected={selectedPlugins}
                      onToggle={(id, checked) => setSelectedPlugins((s) => toggle(s, id, checked))}
                    />
                    {mcps.length + skills.length + plugins.length === 0 && (
                      <EmptyState>No MCPs, skills, or plugins detected</EmptyState>
                    )}
                  </TabsContent>
                );
              })}
            </div>
          </Tabs>
        )}
      </div>

      <div className="flex shrink-0 justify-end px-5 pt-4 pb-5">
        <Button onClick={confirm} componentId="onboarding.import.confirm">
          Confirm
        </Button>
      </div>
    </>
  );
}

export default ImportContextModal;
