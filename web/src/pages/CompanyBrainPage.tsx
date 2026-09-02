import { useEffect, useMemo, useState } from "react";
import {
  BrainCircuitIcon,
  CheckCircle2Icon,
  CloudIcon,
  ExternalLinkIcon,
  FileTextIcon,
  LinkIcon,
  MessageSquareIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  UnplugIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import {
  useActivateCompanyBrainResources,
  useCompanyBrain,
  useCompanyBrainProviders,
  useCompanyBrainResources,
  useDisconnectCompanyBrainConnection,
  usePreviewCompanyBrainResources,
  useStartCompanyBrainOAuth,
  useSyncCompanyBrainSelection,
  useUpdateCompanyBrainSelection,
} from "@/hooks/useCompanyBrain";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import type {
  BrainPreviewDocument,
  CompanyBrainProvider,
  IntegrationConnection,
  IntegrationSelection,
} from "@/lib/companyBrainApi";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

const SCHEDULES = {
  manual: null,
  daily: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  weekdays: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0",
} as const;

const PROVIDER_META = {
  google: { label: "Google Workspace", icon: CloudIcon },
  slack: { label: "Slack", icon: MessageSquareIcon },
  notion: { label: "Notion", icon: FileTextIcon },
} as const;

function sourceStatus(selection: IntegrationSelection) {
  if (selection.lastError) return { label: "Needs attention", variant: "destructive" as const };
  if (selection.state === "paused") return { label: "Paused", variant: "secondary" as const };
  if (selection.state === "disconnected") {
    return { label: "Disconnected", variant: "outline" as const };
  }
  return { label: "Connected", variant: "outline" as const };
}

function scheduleKey(rrule: string | null): keyof typeof SCHEDULES {
  const match = Object.entries(SCHEDULES).find(([, value]) => value === rrule);
  return (match?.[0] as keyof typeof SCHEDULES | undefined) ?? "manual";
}

export function CompanyBrainPage() {
  const isAdmin = useIsAdmin();
  const brain = useCompanyBrain(isAdmin);
  const sync = useSyncCompanyBrainSelection();
  const update = useUpdateCompanyBrainSelection();
  const disconnect = useDisconnectCompanyBrainConnection();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [disconnectCandidate, setDisconnectCandidate] = useState<string | null>(null);

  if (!isAdmin) {
    return (
      <PageScroll contentClassName="px-8" extraBottom="2.5rem">
        <h1 className="text-2xl font-semibold">Company brain</h1>
        <p className="mt-2 text-ui text-muted-foreground">
          You don't have permission to manage company knowledge sources.
        </p>
      </PageScroll>
    );
  }

  if (brain.isLoading || !brain.data) {
    return (
      <div className="flex min-h-full items-center justify-center text-muted-foreground">
        <Spinner />
      </div>
    );
  }

  const { installation, connections, selections, runs } = brain.data;
  const connectionById = new Map(connections.map((item) => [item.id, item]));

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Company brain</h1>
          <p className="mt-1 text-ui text-muted-foreground">
            Shared knowledge available to your custom agents.
          </p>
        </div>
        <Button onClick={() => setWizardOpen(true)}>
          <PlusIcon /> Connect source
        </Button>
      </div>

      <section className="mt-7 border-y border-border py-4">
        <div className="grid gap-4 sm:grid-cols-3">
          <StatusMetric
            label="Brain health"
            value={installation?.status === "ready" ? "Ready" : "Not provisioned"}
            healthy={installation?.status === "ready"}
          />
          <StatusMetric label="Connected sources" value={String(selections.length)} />
          <StatusMetric
            label="Last successful sync"
            value={formatLastSync(selections)}
            title={formatLastSyncAbsolute(selections)}
          />
        </div>
        {installation?.repoUrl && (
          <a
            href={installation.repoUrl}
            target="_blank"
            rel="noreferrer"
            className="mt-4 inline-flex items-center gap-1 text-ui text-primary hover:underline"
          >
            <LinkIcon className="ui-icon" /> Open company-owned repository
            <ExternalLinkIcon className="size-3.5" />
          </a>
        )}
      </section>

      <section className="mt-8">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Connected sources</h2>
          <Button variant="ghost" size="sm" onClick={() => void brain.refetch()}>
            <RefreshCwIcon /> Refresh
          </Button>
        </div>
        {selections.length === 0 ? (
          <div className="border-y border-border py-10 text-center text-ui text-muted-foreground">
            No sources connected.
          </div>
        ) : (
          <div className="divide-y divide-border border-y border-border">
            {selections.map((selection) => (
              <SourceRow
                key={selection.id}
                selection={selection}
                connection={connectionById.get(selection.connectionId)}
                pending={sync.isPending || update.isPending || disconnect.isPending}
                onSync={(retry) => void sync.mutateAsync({ id: selection.id, retry })}
                onToggle={() =>
                  void update.mutateAsync({
                    id: selection.id,
                    input: { state: selection.state === "paused" ? "active" : "paused" },
                  })
                }
                onSchedule={(rrule) =>
                  void update.mutateAsync({ id: selection.id, input: { rrule, timezone: "UTC" } })
                }
                onDisconnect={() => setDisconnectCandidate(selection.connectionId)}
              />
            ))}
          </div>
        )}
      </section>

      <section className="mt-8">
        <h2 className="mb-3 text-lg font-semibold">Sync activity</h2>
        <div className="divide-y divide-border border-y border-border">
          {runs.slice(0, 8).map((run) => {
            const selection = selections.find((item) => item.id === run.selectionId);
            return (
              <div key={run.id} className="grid gap-1 py-3 text-ui sm:grid-cols-[1fr_auto_auto]">
                <div>
                  <span className="font-medium">{selection?.resourceName ?? "Source"}</span>
                  {run.error && <p className="mt-1 text-sm text-destructive">{run.error}</p>}
                </div>
                <span className="text-muted-foreground">
                  {run.changedCount} changed · {run.deletedCount} deleted
                </span>
                <Badge variant={run.status === "failed" ? "destructive" : "outline"}>
                  {run.status}
                </Badge>
              </div>
            );
          })}
          {runs.length === 0 && (
            <p className="py-6 text-center text-ui text-muted-foreground">No sync runs yet.</p>
          )}
        </div>
      </section>

      <ConnectSourceDialog
        open={wizardOpen}
        onOpenChange={setWizardOpen}
        connections={connections}
        onActivated={() => void brain.refetch()}
      />
      <Dialog
        open={disconnectCandidate !== null}
        onOpenChange={(next) => !next && setDisconnectCandidate(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disconnect this source?</DialogTitle>
            <DialogDescription>
              Future syncs will stop and the credential will be removed. Existing Git history and
              indexed knowledge are retained.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDisconnectCandidate(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={disconnect.isPending}
              onClick={() => {
                if (disconnectCandidate === null) return;
                void disconnect.mutateAsync(disconnectCandidate).then(() => {
                  setDisconnectCandidate(null);
                });
              }}
            >
              <UnplugIcon /> Disconnect
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}

function StatusMetric({
  label,
  value,
  healthy,
  title,
}: {
  label: string;
  value: string;
  healthy?: boolean;
  title?: string;
}) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      <div className="mt-1 flex items-center gap-2" title={title}>
        {healthy && <CheckCircle2Icon className="size-4 text-status-green" />}
        <span className="text-ui font-medium">{value}</span>
      </div>
    </div>
  );
}

function SourceRow({
  selection,
  connection,
  pending,
  onSync,
  onToggle,
  onSchedule,
  onDisconnect,
}: {
  selection: IntegrationSelection;
  connection?: IntegrationConnection;
  pending: boolean;
  onSync: (retry: boolean) => void;
  onToggle: () => void;
  onSchedule: (rrule: string | null) => void;
  onDisconnect: () => void;
}) {
  const provider = connection?.provider ?? "notion";
  const Icon = PROVIDER_META[provider].icon;
  const status = sourceStatus(selection);
  return (
    <div className="grid gap-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-muted">
          <Icon className="size-4 text-muted-foreground" />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-ui font-medium">{selection.resourceName}</span>
            <Badge variant={status.variant}>{status.label}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {PROVIDER_META[provider].label} · {selection.pageCount} pages · Last sync{" "}
            {selection.lastSyncedAt ? relativeTime(selection.lastSyncedAt * 1000) : "never"}
          </p>
          {selection.lastError && (
            <p className="mt-1 line-clamp-2 text-sm text-destructive">{selection.lastError}</p>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <Select
          value={scheduleKey(selection.rrule)}
          onValueChange={(value) => {
            const rule = SCHEDULES[value as keyof typeof SCHEDULES];
            onSchedule(rule);
          }}
          disabled={pending || selection.state === "disconnected"}
        >
          <SelectTrigger className="h-8 w-28" aria-label="Sync schedule">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="manual">Manual</SelectItem>
            <SelectItem value="daily">Daily</SelectItem>
            <SelectItem value="weekdays">Weekdays</SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="ghost"
          size="icon-sm"
          title="Sync now"
          onClick={() => onSync(false)}
          disabled={pending || selection.state === "disconnected"}
        >
          <RotateCcwIcon />
        </Button>
        {selection.lastError && (
          <Button
            variant="ghost"
            size="icon-sm"
            title="Retry sync"
            onClick={() => onSync(true)}
            disabled={pending}
          >
            <RefreshCwIcon />
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon-sm"
          title={selection.state === "paused" ? "Resume source" : "Pause source"}
          onClick={onToggle}
          disabled={pending || selection.state === "disconnected"}
        >
          {selection.state === "paused" ? <PlayIcon /> : <PauseIcon />}
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          title="Disconnect source"
          onClick={onDisconnect}
          disabled={pending || selection.state === "disconnected"}
        >
          <UnplugIcon />
        </Button>
      </div>
    </div>
  );
}

function ConnectSourceDialog({
  open,
  onOpenChange,
  connections,
  onActivated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  connections: IntegrationConnection[];
  onActivated: () => void;
}) {
  const providers = useCompanyBrainProviders();
  const oauth = useStartCompanyBrainOAuth();
  const preview = usePreviewCompanyBrainResources();
  const activate = useActivateCompanyBrainResources();
  const [provider, setProvider] = useState<CompanyBrainProvider | null>(null);
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [documents, setDocuments] = useState<BrainPreviewDocument[]>([]);
  const [activeDocument, setActiveDocument] = useState(0);
  const [schedule, setSchedule] = useState<keyof typeof SCHEDULES>("daily");
  const resources = useCompanyBrainResources(connectionId);

  useEffect(() => {
    if (!provider || connectionId) return;
    const existing = [...connections]
      .reverse()
      .find((item) => item.provider === provider && item.status === "connected");
    if (existing) setConnectionId(existing.id);
  }, [connections, connectionId, provider]);

  useEffect(() => {
    if (!open) return;
    function onOAuthConnected(event: MessageEvent<unknown>) {
      if (event.origin !== window.location.origin || typeof event.data !== "object") return;
      const data = event.data as {
        type?: string;
        provider?: CompanyBrainProvider;
        connectionId?: string;
      };
      if (data.type !== "company-brain.oauth.connected" || !data.provider || !data.connectionId) {
        return;
      }
      setProvider(data.provider);
      setConnectionId(data.connectionId);
      onActivated();
    }
    window.addEventListener("message", onOAuthConnected);
    return () => window.removeEventListener("message", onOAuthConnected);
  }, [onActivated, open]);

  const selectedResources = useMemo(
    () => (resources.data ?? []).filter((item) => selectedIds.includes(item.id)),
    [resources.data, selectedIds],
  );

  function reset() {
    setProvider(null);
    setConnectionId(null);
    setSelectedIds([]);
    setDocuments([]);
    setActiveDocument(0);
    setSchedule("daily");
  }

  async function chooseProvider(next: CompanyBrainProvider) {
    setProvider(next);
    const existing = [...connections]
      .reverse()
      .find((item) => item.provider === next && item.status === "connected");
    if (existing) {
      setConnectionId(existing.id);
      return;
    }
    const result = await oauth.mutateAsync(next);
    window.open(result.authorizeUrl, "company-brain-oauth", "popup,width=620,height=760");
  }

  async function buildPreview() {
    if (!connectionId) return;
    const next = await preview.mutateAsync({ connectionId, resources: selectedResources });
    setDocuments(next);
    setActiveDocument(0);
  }

  async function activateSelection() {
    if (!connectionId) return;
    await activate.mutateAsync({
      connectionId,
      resources: selectedResources,
      rrule: SCHEDULES[schedule],
    });
    onActivated();
    reset();
    onOpenChange(false);
  }

  const current = documents[activeDocument];
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !activate.isPending) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent
        className={cn(
          "max-h-[88vh] overflow-y-auto",
          connectionId && documents.length === 0 && "sm:max-w-2xl",
          documents.length > 0 && "w-[calc(100%-2rem)] sm:max-w-4xl",
        )}
      >
        <DialogHeader>
          <DialogTitle>Connect a company source</DialogTitle>
          <DialogDescription>
            {documents.length > 0
              ? "Review transformed pages before activation."
              : connectionId
                ? "Choose organization-shared resources."
                : "Choose a provider."}
          </DialogDescription>
        </DialogHeader>

        {!provider && (
          <div className="divide-y divide-border border-y border-border">
            {(providers.data ?? []).map((item) => {
              const meta = PROVIDER_META[item.id];
              const Icon = meta.icon;
              return (
                <button
                  key={item.id}
                  type="button"
                  className="flex w-full items-center gap-3 px-1 py-4 text-left hover:bg-muted/50 disabled:opacity-50"
                  onClick={() => void chooseProvider(item.id)}
                  disabled={!item.configured || oauth.isPending}
                >
                  <span className="flex size-9 items-center justify-center rounded-md bg-muted">
                    <Icon className="size-4" />
                  </span>
                  <span className="flex-1 font-medium">{meta.label}</span>
                  <Badge variant={item.configured ? "outline" : "secondary"}>
                    {item.configured ? "Available" : "Not configured"}
                  </Badge>
                </button>
              );
            })}
          </div>
        )}

        {provider && !connectionId && (
          <div className="flex min-h-44 flex-col items-center justify-center gap-3 text-center">
            <BrainCircuitIcon className="size-7 text-muted-foreground" />
            <p className="text-ui font-medium">Complete the connection in the new window.</p>
            <Button variant="outline" onClick={() => window.location.reload()}>
              <RefreshCwIcon /> Refresh connection
            </Button>
          </div>
        )}

        {connectionId && documents.length === 0 && (
          <div className="space-y-4">
            <div className="max-h-72 divide-y divide-border overflow-y-auto border-y border-border">
              {(resources.data ?? []).map((resource) => (
                <label key={resource.id} className="flex cursor-pointer items-center gap-3 py-3">
                  <input
                    type="checkbox"
                    className="size-4 accent-primary"
                    checked={selectedIds.includes(resource.id)}
                    onChange={(event) =>
                      setSelectedIds((currentIds) =>
                        event.target.checked
                          ? [...currentIds, resource.id]
                          : currentIds.filter((id) => id !== resource.id),
                      )
                    }
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-ui font-medium">{resource.name}</span>
                    <span className="text-sm text-muted-foreground">
                      {resource.resourceType.replaceAll("_", " ")}
                    </span>
                  </span>
                  <Badge variant="outline">Org shared</Badge>
                </label>
              ))}
              {resources.isLoading && (
                <div className="flex justify-center py-8">
                  <Spinner />
                </div>
              )}
            </div>
            <div className="flex justify-end">
              <Button
                onClick={() => void buildPreview()}
                disabled={selectedResources.length === 0 || preview.isPending}
              >
                {preview.isPending && <Spinner />} Preview pages
              </Button>
            </div>
          </div>
        )}

        {current && (
          <div className="grid min-h-80 gap-4 md:grid-cols-[14rem_minmax(0,1fr)]">
            <div className="divide-y divide-border border-y border-border">
              {documents.map((document, index) => (
                <button
                  key={document.contentSha256}
                  type="button"
                  className={cn(
                    "w-full px-2 py-3 text-left text-ui",
                    index === activeDocument && "bg-muted font-medium",
                  )}
                  onClick={() => setActiveDocument(index)}
                >
                  <span className="line-clamp-2">{document.title}</span>
                </button>
              ))}
            </div>
            <div className="min-w-0 border-y border-border py-3">
              <div className="mb-3 flex items-center justify-between gap-2">
                <h3 className="truncate font-semibold">{current.title}</h3>
                <Badge variant="outline">Org shared</Badge>
              </div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap wrap-break-word text-sm text-muted-foreground">
                {current.markdown}
              </pre>
            </div>
          </div>
        )}

        {documents.length > 0 && (
          <DialogFooter className="items-center sm:justify-between">
            <Select
              value={schedule}
              onValueChange={(value) => setSchedule(value as keyof typeof SCHEDULES)}
            >
              <SelectTrigger className="w-36" aria-label="Initial sync schedule">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="manual">Manual only</SelectItem>
                <SelectItem value="daily">Daily</SelectItem>
                <SelectItem value="weekdays">Weekdays</SelectItem>
              </SelectContent>
            </Select>
            <Button onClick={() => void activateSelection()} disabled={activate.isPending}>
              {activate.isPending && <Spinner />} Activate {selectedResources.length} source
              {selectedResources.length === 1 ? "" : "s"}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

function formatLastSync(selections: IntegrationSelection[]): string {
  const latest = Math.max(...selections.map((item) => item.lastSyncedAt ?? 0));
  return latest > 0 ? relativeTime(latest * 1000) : "Never";
}

function formatLastSyncAbsolute(selections: IntegrationSelection[]): string | undefined {
  const latest = Math.max(...selections.map((item) => item.lastSyncedAt ?? 0));
  return latest > 0 ? absoluteTime(latest * 1000) : undefined;
}
