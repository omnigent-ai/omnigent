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

/** Shared row chrome so the select-all and item rows keep one divider/gap contract. */
function ImportRow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <li className={cn("flex items-center gap-3 border-b border-border last:border-b-0", className)}>
      {children}
    </li>
  );
}

/** One-line credential summary; the harness name is already on the tab. */
function CredentialLine({ source }: { source: string }) {
  return (
    <div className="flex items-center gap-2 py-3 text-xs">
      <span className="shrink-0 text-muted-foreground">Credential</span>
      <span className="min-w-0 flex-1 truncate font-medium text-foreground">{source}</span>
      <span className="flex shrink-0 items-center gap-1 text-muted-foreground">
        <Check className="size-3.5 text-success" aria-hidden="true" />
        Imported
      </span>
    </div>
  );
}

type AssetKind = "mcps" | "skills" | "plugins";

interface SelectableRow {
  id: string;
  name: string;
  metadata?: string;
}

interface AssetList {
  kind: AssetKind;
  label: string;
  rows: SelectableRow[];
}

function countLabel(count: number | undefined, noun: string): string | undefined {
  if (count == null) return undefined;
  return `${count} ${count === 1 ? noun : `${noun}s`}`;
}

/** The harness's non-empty asset lists, in MCPs → Skills → Plugins order. */
function assetLists(context: ImportContext, harness: ImportHarness): AssetList[] {
  const own = <T extends { harness: ImportHarness }>(items: T[]) =>
    items.filter((item) => item.harness === harness);
  const lists: AssetList[] = [
    {
      kind: "mcps",
      label: "MCPs",
      rows: own(context.mcps).map((mcp) => ({
        id: mcp.id,
        name: mcp.name,
        metadata: countLabel(mcp.toolCount, "tool"),
      })),
    },
    {
      kind: "skills",
      label: "Skills",
      rows: own(context.skills).map((skill) => ({ id: skill.id, name: `$${skill.name}` })),
    },
    {
      kind: "plugins",
      label: "Plugins",
      rows: own(context.plugins).map((plugin) => ({
        id: plugin.id,
        name: plugin.name,
        metadata: countLabel(plugin.skillCount, "skill"),
      })),
    },
  ];
  return lists.filter((list) => list.rows.length > 0);
}

/** Checkbox list with a select-all header row. */
function AssetRows({
  list,
  selected,
  onChange,
}: {
  list: AssetList;
  selected: ReadonlySet<string>;
  onChange: (ids: string[], checked: boolean) => void;
}) {
  const idPrefix = useId();
  const allIds = list.rows.map((row) => row.id);
  const selectedCount = allIds.filter((id) => selected.has(id)).length;
  const allState =
    selectedCount === allIds.length ? true : selectedCount === 0 ? false : "indeterminate";
  const componentId = `onboarding.import.${list.kind}`;
  return (
    <ul>
      <ImportRow className="py-2">
        <Checkbox
          id={`${idPrefix}-all`}
          componentId={`${componentId}.all`}
          checked={allState}
          onCheckedChange={(checked) => onChange(allIds, checked === true)}
        />
        <label
          htmlFor={`${idPrefix}-all`}
          className="min-w-0 flex-1 cursor-pointer text-xs font-medium text-muted-foreground"
        >
          Select all
        </label>
        <span className="shrink-0 text-xs text-muted-foreground">
          {selectedCount} of {allIds.length} selected
        </span>
      </ImportRow>
      {list.rows.map(({ id, name, metadata }, index) => {
        const inputId = `${idPrefix}-${index}`;
        return (
          <ImportRow key={id} className="py-2">
            <Checkbox
              id={inputId}
              componentId={componentId}
              checked={selected.has(id)}
              onCheckedChange={(checked) => onChange([id], checked === true)}
            />
            <label
              htmlFor={inputId}
              className="min-w-0 flex-1 cursor-pointer truncate text-ui font-medium text-foreground"
            >
              {name}
            </label>
            {metadata && <span className="shrink-0 text-xs text-muted-foreground">{metadata}</span>}
          </ImportRow>
        );
      })}
    </ul>
  );
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

/** A harness tab: credential line, MCPs / Skills / Plugins switcher, scrolling list. */
function HarnessPanel({
  context,
  harness,
  selection,
  onChange,
}: {
  context: ImportContext;
  harness: ImportHarness;
  selection: Record<AssetKind, ReadonlySet<string>>;
  onChange: (kind: AssetKind, ids: string[], checked: boolean) => void;
}) {
  const credential = context.credentials.find((c) => c.harness === harness);
  const lists = assetLists(context, harness);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {credential && <CredentialLine source={credential.source} />}
      {lists.length === 0 ? (
        <EmptyState>No MCPs, skills, or plugins detected</EmptyState>
      ) : (
        <Tabs
          defaultValue={lists[0].kind}
          componentId="onboarding.import.assetTabs"
          className="min-h-0 flex-1 gap-0 overflow-hidden rounded-lg border border-border"
        >
          {/* The pills head a bordered card so the list reads as their content. */}
          <TabsList
            aria-label="Asset type"
            variant="pill"
            className="w-full shrink-0 justify-start gap-1 rounded-none border-b border-border p-1.5"
          >
            {lists.map((list) => (
              <TabsTrigger
                key={list.kind}
                value={list.kind}
                className="h-7 flex-none gap-1 px-2.5 text-xs"
              >
                {list.label}{" "}
                <span className="text-muted-foreground/70 tabular-nums">{list.rows.length}</span>
              </TabsTrigger>
            ))}
          </TabsList>
          {lists.map((list) => (
            <TabsContent
              key={list.kind}
              value={list.kind}
              className="no-scrollbar min-h-0 overflow-y-auto px-3"
            >
              <AssetRows
                list={list}
                selected={selection[list.kind]}
                onChange={(ids, checked) => onChange(list.kind, ids, checked)}
              />
            </TabsContent>
          ))}
        </Tabs>
      )}
    </div>
  );
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
  const [selection, setSelection] = useState<Record<AssetKind, ReadonlySet<string>>>(() => ({
    mcps: allIds(context.mcps),
    skills: allIds(context.skills),
    plugins: allIds(context.plugins),
  }));
  const harnesses = detectedHarnesses(context);

  const setChecked = (kind: AssetKind, ids: string[], checked: boolean) =>
    setSelection((current) => {
      const next = new Set(current[kind]);
      for (const id of ids) {
        if (checked) next.add(id);
        else next.delete(id);
      }
      return { ...current, [kind]: next };
    });

  const confirm = () => {
    const kept = (items: { id: string }[], selected: ReadonlySet<string>) =>
      items.filter((item) => selected.has(item.id)).map((item) => item.id);
    onConfirm({
      mcps: kept(context.mcps, selection.mcps),
      skills: kept(context.skills, selection.skills),
      plugins: kept(context.plugins, selection.plugins),
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
            className="mt-5 min-h-48 flex-1 gap-0"
          >
            <TabsList
              aria-label="Harness"
              variant="line"
              className="h-9 w-full shrink-0 justify-start gap-4 rounded-none border-b border-border p-0"
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
            {harnesses.map((harness) => (
              <TabsContent key={harness} value={harness} className="flex min-h-0 flex-col">
                <HarnessPanel
                  context={context}
                  harness={harness}
                  selection={selection}
                  onChange={setChecked}
                />
              </TabsContent>
            ))}
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
