import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { BotIcon, PlusIcon, SearchIcon, TrashIcon } from "lucide-react";
import { Link, useNavigate } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { customAgentsSettingsEnabled } from "@/lib/capabilities";
import { useAcpHarnessIds, useHarnessLabels } from "@/lib/agentLabels";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";
import { isAcpHarnessAgent } from "@/lib/agentGrouping";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CreateAgentForm } from "@/shell/CreateAgentDialog";
import { useSettingsRoute } from "@/shell/settingsNav";
import {
  useCustomAgents,
  useSaveCustomAgent,
  useDeleteCustomAgent,
  type ManagedAgent,
} from "@/hooks/useCustomAgents";
import { BackButton } from "./HarnessCatalog";

const BASE = "/settings/custom-agents";
const agentPath = (id: string) => `${BASE}/${encodeURIComponent(id)}`;
const date = (value: number | null) =>
  value === null ? "—" : new Date(value * 1000).toLocaleDateString();

export function SettingsCustomAgentsSection() {
  const enabled = customAgentsSettingsEnabled(useServerInfo());
  const { agentId } = useSettingsRoute();
  const navigate = useNavigate();
  const client = useQueryClient();
  const servers = useCustomAgents("server", enabled);
  const yours = useCustomAgents("user", enabled);
  const save = useSaveCustomAgent();
  const labels = useHarnessLabels(enabled);
  const acpHarnesses = useAcpHarnessIds(enabled);
  const [search, setSearch] = useState("");
  const serverAgents = servers.data?.pages.flatMap((page) => page.data) ?? [];
  const userAgents = yours.data?.pages.flatMap((page) => page.data) ?? [];
  const owned = userAgents.find((a) => a.id === agentId);
  const detail = owned ?? serverAgents.find((a) => a.id === agentId);
  const detailId = agentId === "new" ? undefined : agentId;

  // ponytail: bookmarks scan list pages until a by-ID API is available.
  useEffect(() => {
    if (!enabled || !detailId || detail) return;
    if (yours.hasNextPage && !yours.isFetching && !yours.isError) void yours.fetchNextPage();
    if (servers.hasNextPage && !servers.isFetching && !servers.isError)
      void servers.fetchNextPage();
  }, [enabled, detailId, detail, yours, servers]);

  if (!enabled) return null;
  const back = (
    <BackButton label="Custom agents" to={BASE} componentId="settings.custom_agents.back" />
  );
  if (agentId === "new") {
    return (
      <div className="@container">
        {back}
        <h1 className="mb-6 text-2xl font-semibold">Create custom agent</h1>
        <CreateAgentForm
          submitLabel="Save agent"
          notice="Saving an existing installed agent's name replaces its entire configuration. Sessions using that agent will receive the update. Use a new name to keep both agents."
          onCancel={() => navigate(BASE)}
          onCreate={async (input) => {
            const agent = await save.mutateAsync(input);
            navigate(agentPath(agent.id));
          }}
        />
      </div>
    );
  }
  if (detailId) {
    return (
      <div className="@container">
        {back}
        {detail ? (
          <AgentDetail
            key={detail.id}
            agent={detail}
            owned={!!owned}
            harness={labels[detail.harness ?? ""] ?? detail.harness ?? "Unknown"}
            onDeleted={() => navigate(BASE)}
          />
        ) : (
          <>
            <h1 className="mb-6 text-2xl font-semibold">Custom agent</h1>
            {servers.isError || yours.isError ? (
              <p role="alert">
                Could not load this agent.{" "}
                <Button
                  variant="link"
                  onClick={() => void client.resetQueries({ queryKey: ["settings-agents"] })}
                >
                  Retry
                </Button>
              </p>
            ) : (
              <p className="text-ui text-muted-foreground">
                {servers.isFetching ||
                yours.isFetching ||
                servers.isPending ||
                yours.isPending ||
                servers.hasNextPage ||
                yours.hasNextPage
                  ? "Loading agent…"
                  : "Agent not found. It may have been removed or belong to another user."}
              </p>
            )}
          </>
        )}
      </div>
    );
  }
  const matches = (a: ManagedAgent) =>
    `${a.name} ${a.description ?? ""}`.toLowerCase().includes(search.trim().toLowerCase());
  const builtins = serverAgents
    .filter(
      (a) =>
        !(
          a.builtin &&
          (nativeCodingAgentForAgentName(a.name) ||
            isAcpHarnessAgent({
              harness: a.harness,
              acpHarness: acpHarnesses.has(a.harness ?? "") || undefined,
            }))
        ),
    )
    .filter(matches);
  const filtered = userAgents.filter(matches);
  return (
    <div className="@container">
      <h1 className="pb-6 text-2xl font-semibold">Custom agents</h1>
      <div className="mb-6 flex items-center gap-2">
        <div className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg border border-border px-2.5">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <input
            type="search"
            aria-label="Search agents"
            placeholder="Search agents…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground/50"
          />
        </div>
        <Button asChild size="sm">
          <Link to={`${BASE}/new`} componentId="settings.custom_agents.create">
            <PlusIcon />
            Create
          </Link>
        </Button>
      </div>
      {(yours.hasNextPage || servers.hasNextPage) && (
        <p className="mb-4 text-xs text-muted-foreground">
          Search filters loaded agents. Load more to include older agents.
        </p>
      )}
      <section className="mb-8" aria-label="Built-in agents">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Built-in &amp; server agents
        </h2>
        <div className="grid gap-3 @lg:grid-cols-2">
          {builtins.map((agent) => (
            <Link
              key={agent.id}
              to={agentPath(agent.id)}
              componentId="settings.custom_agents.review_server"
              className="flex flex-col gap-3 rounded-xl border border-border p-4 hover:bg-muted/40"
            >
              <span className="flex items-center gap-3 font-medium">
                <BotIcon className="size-5 shrink-0" aria-hidden />
                {agent.name}
              </span>
              <span className="text-ui text-muted-foreground">
                {agent.description || "Server-provided agent"}
              </span>
            </Link>
          ))}
        </div>
        <ListStatus
          query={servers}
          scope="server"
          empty={builtins.length === 0}
          emptyText={search ? "No matching server agents." : "No server agents."}
        />
      </section>
      <section aria-label="Your agents">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Yours
        </h2>
        {filtered.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-ui">
              <thead>
                <tr className="border-b border-border">
                  <th className="py-3 pr-4 font-medium">Name</th>
                  <th className="px-3 py-3 font-medium">Harness</th>
                  <th className="whitespace-nowrap px-3 py-3 font-medium">Created</th>
                  <th className="whitespace-nowrap py-3 pl-3 font-medium">Last edited</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((agent) => (
                  <tr key={agent.id} className="border-b border-border last:border-0">
                    <td className="max-w-xs py-3 pr-4">
                      <Link
                        to={agentPath(agent.id)}
                        componentId="settings.custom_agents.review"
                        className="font-medium hover:underline"
                      >
                        {agent.name}
                      </Link>
                      <p className="truncate text-xs text-muted-foreground">{agent.description}</p>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3">
                      {labels[agent.harness ?? ""] ?? agent.harness ?? "Unknown"}
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">
                      {date(agent.created_at)}
                    </td>
                    <td className="whitespace-nowrap py-3 pl-3 text-muted-foreground">
                      {date(agent.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ListStatus
          query={yours}
          scope="user"
          empty={filtered.length === 0}
          emptyText={
            search
              ? "No matching agents in the loaded results."
              : "No custom agents yet. Create an agent to keep it available for new sessions."
          }
        />
      </section>
    </div>
  );
}

function ListStatus({
  query,
  scope,
  empty,
  emptyText,
}: {
  query: ReturnType<typeof useCustomAgents>;
  scope: "user" | "server";
  empty: boolean;
  emptyText: string;
}) {
  const client = useQueryClient();
  if (query.isPending) return <p className="py-4 text-ui text-muted-foreground">Loading agents…</p>;
  if (query.isError)
    return (
      <p role="alert" className="py-4 text-ui">
        Could not load agents.{" "}
        <Button
          variant="link"
          onClick={() => void client.resetQueries({ queryKey: ["settings-agents", scope] })}
        >
          Retry
        </Button>
      </p>
    );
  return (
    <>
      {empty && <p className="py-4 text-ui text-muted-foreground">{emptyText}</p>}
      {query.hasNextPage && (
        <Button
          className="mt-4"
          variant="outline"
          loading={query.isFetchingNextPage}
          disabled={query.isFetching}
          onClick={() => void query.fetchNextPage()}
        >
          Load more {scope === "user" ? "agents" : "server agents"}
        </Button>
      )}
    </>
  );
}

function AgentDetail({
  agent,
  owned,
  harness,
  onDeleted,
}: {
  agent: ManagedAgent;
  owned: boolean;
  harness: string;
  onDeleted: () => void;
}) {
  const remove = useDeleteCustomAgent();
  const [confirm, setConfirm] = useState(false);
  const [inUse, setInUse] = useState<string | null>(null);
  async function onRemove() {
    try {
      const count = await remove.mutateAsync({ id: agent.id, force: inUse !== null });
      if (count === null) onDeleted();
      else setInUse(count);
    } catch {
      /* The mutation error is displayed in the dialog. */
    }
  }
  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <h1 className="min-w-0 break-words text-2xl font-semibold">{agent.name}</h1>
        {owned && (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Delete agent"
            onClick={() => {
              remove.reset();
              setInUse(null);
              setConfirm(true);
            }}
          >
            <TrashIcon className="size-4" />
          </Button>
        )}
      </div>
      <p className="mt-3 text-ui text-muted-foreground">{agent.description || "No description."}</p>
      <dl className="my-6 grid grid-cols-2 gap-4 text-ui">
        <div>
          <dt className="text-muted-foreground">Harness</dt>
          <dd>{harness}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Version</dt>
          <dd>{agent.version}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Created</dt>
          <dd>{date(agent.created_at)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Last edited</dt>
          <dd>{date(agent.updated_at)}</dd>
        </div>
      </dl>
      <section className="mb-6">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          MCP servers
        </h2>
        {agent.mcp_servers?.length ? (
          <ul className="space-y-2">
            {agent.mcp_servers.map((server) => (
              <li key={server.name} className="rounded-xl border border-border p-3">
                <p className="text-ui font-medium">{server.name}</p>
                <p className="text-xs text-muted-foreground">
                  {server.transport}
                  {server.description ? ` · ${server.description}` : ""}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-ui text-muted-foreground">No MCP servers listed.</p>
        )}
      </section>
      <section className="mb-6">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Skills
        </h2>
        {agent.skills?.length ? (
          <ul className="space-y-2">
            {agent.skills.map((skill) => (
              <li key={skill.name} className="rounded-xl border border-border p-3">
                <p className="text-ui font-medium">{skill.name}</p>
                <p className="text-xs text-muted-foreground">{skill.description}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-ui text-muted-foreground">No skills listed.</p>
        )}
      </section>
      <p className="text-xs text-muted-foreground">
        {owned
          ? "Configuration editing is not available here yet."
          : "Server-provided agents are read-only."}
      </p>
      <Dialog
        open={confirm}
        onOpenChange={(open) => {
          if (!remove.isPending) setConfirm(open);
        }}
      >
        <DialogContent showCloseButton={!remove.isPending}>
          <DialogHeader>
            <DialogTitle>Delete {agent.name}?</DialogTitle>
            <DialogDescription>
              {inUse === null
                ? "This removes the agent from your collection. This cannot be undone."
                : `${inUse} session(s) still use this agent. Removing it can stop those sessions from continuing. Remove it anyway?`}
            </DialogDescription>
          </DialogHeader>
          {remove.error && (
            <p role="alert" className="text-ui text-destructive">
              {remove.error.message}
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" disabled={remove.isPending} onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={remove.isPending}
              disabled={remove.isPending}
              onClick={() => void onRemove()}
            >
              {inUse === null ? "Delete" : "Remove anyway"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
