// Post-setup "Your setup is ready" modal: one tab per harness, showing the
// credential Omnigent adopted and the MCP servers, skills, and plugins found
// there. Review only: sessions already load these, so nothing is selected.

import type { ReactNode } from "react";
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
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  INVENTORY_HARNESS_IDS,
  type HarnessInventoryContext,
  type HarnessInventoryStatus,
  type InventoryAssetKind,
} from "@/hooks/useHarnessInventory";
import { skillInvocationPrefix } from "@/lib/harnessSetup";
import { Link } from "@/lib/routing";

export type ImportHarness = BrandHarness;
export type ImportContext = HarnessInventoryContext;

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

/** One-line credential summary; the harness name is already on the tab. */
function CredentialLine({ source }: { source: string }) {
  return (
    <div className="flex items-center gap-2 py-3 text-xs">
      <span className="shrink-0 text-muted-foreground">Credential</span>
      <span className="min-w-0 flex-1 truncate font-medium text-foreground">{source}</span>
      <span className="flex shrink-0 items-center gap-1 text-muted-foreground">
        <Check className="size-3.5 text-success" aria-hidden="true" />
        Detected
      </span>
    </div>
  );
}

type AssetKind = "mcps" | "skills" | "plugins";

interface AssetRow {
  id: string;
  name: string;
  metadata?: string;
}

interface AssetList {
  kind: AssetKind;
  label: string;
  rows: AssetRow[];
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
        metadata: mcp.detail,
      })),
    },
    {
      kind: "skills",
      label: "Skills",
      rows: own(context.skills).map((skill) => ({
        id: skill.id,
        name: `${skillInvocationPrefix(INVENTORY_HARNESS_IDS[harness])}${skill.name}`,
      })),
    },
    {
      kind: "plugins",
      label: "Plugins",
      rows: own(context.plugins).map((plugin) => ({
        id: plugin.id,
        name: plugin.name,
        metadata: countLabel(plugin.skills.length || undefined, "skill"),
      })),
    },
  ];
  return lists.filter((list) => list.rows.length > 0);
}

/** Read-only list of one asset type. */
function AssetRows({ list }: { list: AssetList }) {
  return (
    <ul aria-label={list.label}>
      {list.rows.map(({ id, name, metadata }) => (
        <li
          key={id}
          className="flex items-center gap-3 border-b border-border py-2 last:border-b-0"
        >
          <span className="min-w-0 flex-1 truncate text-ui font-medium text-foreground">
            {name}
          </span>
          {metadata && (
            <span className="max-w-[45%] shrink-0 truncate text-xs text-muted-foreground">
              {metadata}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

const UNAVAILABLE_LABEL: Record<InventoryAssetKind, string> = {
  mcps: "MCP servers",
  skills: "skills and plugins",
  plugins: "plugins",
};

/** Names the asset kinds the host couldn't report, e.g. "Couldn't read MCP servers". */
function unavailableNotice(unavailable: InventoryAssetKind[]): string | null {
  if (unavailable.length === 0) return null;
  return `Couldn't read ${unavailable.map((kind) => UNAVAILABLE_LABEL[kind]).join(" or ")} from this machine.`;
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
  notice,
}: {
  context: ImportContext;
  harness: ImportHarness;
  notice: string | null;
}) {
  const credential = context.credentials.find((c) => c.harness === harness);
  const lists = assetLists(context, harness);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {credential && <CredentialLine source={credential.source} />}
      {notice && lists.length > 0 && (
        <p role="status" className="pb-3 text-xs text-muted-foreground">
          {notice}
        </p>
      )}
      {lists.length === 0 ? (
        <EmptyState>{notice ?? "No MCPs, skills, or plugins detected"}</EmptyState>
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
              <AssetRows list={list} />
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
  /** Called when the user confirms; closing with X or Escape doesn't call it. */
  onConfirm: () => void;
  status?: HarnessInventoryStatus;
  /** Machine the harnesses run on; shown when the user has several. */
  hostName?: string;
  /** Asset kinds the host couldn't report. */
  unavailable?: InventoryAssetKind[];
  mcpUnsupported?: boolean;
  /** Replaces the default loading copy, e.g. while the host is still connecting. */
  loadingMessage?: string;
}

export function ImportContextModal({
  open,
  onOpenChange,
  onConfirm,
  ...body
}: ImportContextModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex h-[640px] max-h-[85vh] flex-col gap-0 overflow-hidden rounded-[20px] p-0 sm:max-w-[560px]"
      >
        <ImportContextBody
          {...body}
          onConfirm={() => {
            onConfirm();
            onOpenChange(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

const NONE_UNAVAILABLE: InventoryAssetKind[] = [];

function ImportContextBody({
  context,
  onConfirm,
  status = "ready",
  hostName,
  unavailable = NONE_UNAVAILABLE,
  mcpUnsupported = false,
  loadingMessage = "Checking your harnesses…",
}: Omit<ImportContextModalProps, "open" | "onOpenChange">) {
  const harnesses = detectedHarnesses(context);
  const notice =
    [
      mcpUnsupported && "Please update this host to list MCP servers.",
      unavailableNotice(
        mcpUnsupported ? unavailable.filter((kind) => kind !== "mcps") : unavailable,
      ),
    ]
      .filter(Boolean)
      .join(" ") || null;
  const machine = hostName ?? "This machine";

  let content: ReactNode;
  if (status === "loading") {
    content = (
      <EmptyState>
        <Spinner aria-hidden="true" className="mx-auto mb-2" />
        {loadingMessage}
      </EmptyState>
    );
  } else if (status === "offline") {
    content = <EmptyState>{machine} is offline. Reconnect it to review its imports.</EmptyState>;
  } else if (harnesses.length === 0) {
    content = <EmptyState>{notice ?? "Nothing to import from your harnesses"}</EmptyState>;
  } else {
    content = (
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
            <HarnessPanel context={context} harness={harness} notice={notice} />
          </TabsContent>
        ))}
      </Tabs>
    );
  }

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
            Your setup is ready
          </DialogTitle>
          <DialogDescription className="max-w-[480px] text-[14px] leading-5">
            {hostName ? `Found in your harnesses on ${hostName}.` : "Found in your harnesses."}{" "}
            These carry over automatically.
          </DialogDescription>
        </div>
        {content}
      </div>

      <div className="flex shrink-0 justify-end gap-2 px-5 pt-4 pb-5">
        <DialogClose asChild>
          <Button variant="outline" asChild componentId="onboarding.import.seeMore">
            <Link to="/settings/harnesses">See more</Link>
          </Button>
        </DialogClose>
        <Button onClick={onConfirm} componentId="onboarding.import.confirm">
          Confirm
        </Button>
      </div>
    </>
  );
}

export default ImportContextModal;
