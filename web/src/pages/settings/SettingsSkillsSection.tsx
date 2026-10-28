import { useMemo, useState } from "react";
import { PackageIcon, PlusIcon, RefreshCwIcon, SearchIcon, TrashIcon } from "lucide-react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { skillUrlInstallEnabled, skillsMarketplaceEnabled } from "@/lib/capabilities";
import { useIsAdmin } from "@/hooks/useIsAdmin";
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
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { CatalogSkill, InstalledSkill, SkillCatalog, SkillScope } from "@/lib/skillsApi";
import {
  useAddSkillSource,
  useDeleteInstalledSkill,
  useDeleteSkillSource,
  useInstallSkills,
  useInstalledSkillContent,
  useInstalledSkills,
  usePreviewCatalog,
  useRefreshCatalog,
  useSetInstalledSkillEnabled,
  useSkillSources,
  useSourceCatalog,
  useUpdateInstalledSkill,
} from "@/hooks/useSkillMarketplace";

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : "Something went wrong.";

const shortCommit = (commit: string | null) => (commit ? commit.slice(0, 7) : null);

export function SettingsSkillsSection() {
  const info = useServerInfo();
  const isAdmin = useIsAdmin();
  const [tab, setTab] = useState("installed");
  if (!skillsMarketplaceEnabled(info)) return null;
  return (
    <div className="@container">
      <h1 className="pb-2 text-2xl font-semibold">Skills</h1>
      <p className="mb-6 max-w-2xl text-ui text-muted-foreground">
        Skills installed here are offered in the slash menu of every new session, on any host or
        sandbox. They come from GitHub repositories: a plain skills repo or a Claude Code
        marketplace. Every entry shows the repository and commit it was taken from.
      </p>
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="installed" data-testid="skills-tab-installed">
            Installed
          </TabsTrigger>
          <TabsTrigger value="browse" data-testid="skills-tab-browse">
            Browse
          </TabsTrigger>
        </TabsList>
        <TabsContent value="installed">
          <InstalledSkillsTab isAdmin={isAdmin} />
        </TabsContent>
        <TabsContent value="browse">
          <BrowseTab isAdmin={isAdmin} urlInstall={skillUrlInstallEnabled(info)} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// ── Installed ─────────────────────────────────────────────────────────────

function InstalledSkillsTab({ isAdmin }: { isAdmin: boolean }) {
  const installed = useInstalledSkills(true);
  const toggle = useSetInstalledSkillEnabled();
  const update = useUpdateInstalledSkill();
  const remove = useDeleteInstalledSkill();
  const [viewing, setViewing] = useState<InstalledSkill | null>(null);
  const [removing, setRemoving] = useState<InstalledSkill | null>(null);
  const skills = useMemo(
    () => installed.data?.pages.flatMap((page) => page.data) ?? [],
    [installed.data],
  );
  // The server enforces this on every mutation; hiding the controls spares a
  // regular user a 403 on an "Everyone" skill.
  const canMutate = (skill: InstalledSkill) => isAdmin || skill.scope === "user";

  if (installed.isPending) return <Loading label="Loading installed skills" />;
  if (installed.isError) return <ErrorLine error={installed.error} />;
  if (skills.length === 0) {
    return (
      <p className="py-6 text-ui text-muted-foreground" data-testid="installed-skills-empty">
        Nothing installed yet. Browse a repository to add skills.
      </p>
    );
  }
  // A failed removal is shown inside its confirmation dialog, where the user is.
  const mutationError = toggle.error ?? update.error;
  return (
    <div className="flex flex-col gap-3" data-testid="installed-skills">
      {mutationError && <ErrorLine error={mutationError} />}
      {skills.map((skill) => (
        <div
          key={skill.id}
          data-testid={`installed-skill-${skill.name}`}
          className="flex items-start justify-between gap-4 rounded-xl border border-border p-4"
        >
          <div className="flex min-w-0 flex-col gap-1">
            <button
              type="button"
              className="truncate text-left font-medium hover:underline"
              onClick={() => setViewing(skill)}
            >
              {skill.display_name ?? skill.name}
            </button>
            <span className="text-ui text-muted-foreground">{skill.description}</span>
            <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{skill.source}</span>
              {skill.ref && <span>@{skill.ref}</span>}
              {shortCommit(skill.commit) && <code>{shortCommit(skill.commit)}</code>}
              <Badge variant="secondary">{skill.scope === "org" ? "Everyone" : "Only you"}</Badge>
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {canMutate(skill) ? (
              <>
                <Switch
                  aria-label={`Enable ${skill.name}`}
                  data-testid={`installed-skill-toggle-${skill.name}`}
                  checked={skill.enabled}
                  disabled={toggle.isPending}
                  onCheckedChange={(next) => toggle.mutate({ id: skill.id, enabled: next })}
                  componentId="settings.skills.toggle"
                />
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Update ${skill.name}`}
                  disabled={update.isPending}
                  onClick={() => update.mutate(skill.id)}
                  componentId="settings.skills.update"
                >
                  <RefreshCwIcon />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove ${skill.name}`}
                  onClick={() => setRemoving(skill)}
                  componentId="settings.skills.remove"
                >
                  <TrashIcon />
                </Button>
              </>
            ) : (
              <span
                className="text-xs text-muted-foreground"
                data-testid={`installed-skill-managed-${skill.name}`}
              >
                {skill.enabled ? "On for everyone" : "Off for everyone"} · managed by an
                administrator
              </span>
            )}
          </div>
        </div>
      ))}
      {installed.hasNextPage && (
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          disabled={installed.isFetchingNextPage}
          onClick={() => void installed.fetchNextPage()}
          componentId="settings.skills.load_more"
        >
          Load more
        </Button>
      )}
      <SkillContentDialog skill={viewing} onClose={() => setViewing(null)} />
      <Dialog open={removing !== null} onOpenChange={(open) => !open && setRemoving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {removing?.name}?</DialogTitle>
            <DialogDescription>
              New sessions stop offering it. Sessions already running keep the copy they started
              with.
            </DialogDescription>
          </DialogHeader>
          {remove.error && <ErrorLine error={remove.error} />}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              data-testid="confirm-remove-skill"
              disabled={remove.isPending}
              onClick={() => {
                if (removing) remove.mutate(removing.id, { onSuccess: () => setRemoving(null) });
              }}
              componentId="settings.skills.remove_confirm"
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function SkillContentDialog({
  skill,
  onClose,
}: {
  skill: InstalledSkill | null;
  onClose: () => void;
}) {
  const content = useInstalledSkillContent(skill?.id);
  return (
    <Dialog open={skill !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{skill?.display_name ?? skill?.name}</DialogTitle>
          <DialogDescription>
            {skill?.source}
            {skill?.path ? ` · ${skill.path}` : ""}
          </DialogDescription>
        </DialogHeader>
        {content.isPending ? (
          <Loading label="Loading skill" />
        ) : content.isError ? (
          <ErrorLine error={content.error} />
        ) : (
          <pre
            data-testid="skill-content"
            className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-3 text-xs"
          >
            {content.data}
          </pre>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ── Browse ────────────────────────────────────────────────────────────────

function BrowseTab({ isAdmin, urlInstall }: { isAdmin: boolean; urlInstall: boolean }) {
  const sources = useSkillSources(true);
  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);
  const [preview, setPreview] = useState<SkillCatalog | null>(null);
  const list = useMemo(
    () => sources.data?.pages.flatMap((page) => page.data) ?? [],
    [sources.data],
  );
  const activeId = selectedId ?? list[0]?.id;
  const catalog = useSourceCatalog(preview ? undefined : activeId, true);
  const refresh = useRefreshCatalog();
  const shown = preview ?? catalog.data ?? null;

  return (
    <div className="grid gap-6 @3xl:grid-cols-[16rem_1fr]">
      <aside className="flex flex-col gap-3">
        <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Sources
        </h2>
        {sources.isPending && <Loading label="Loading sources" />}
        {sources.isError && <ErrorLine error={sources.error} />}
        <ul className="flex flex-col gap-1" data-testid="skill-sources">
          {list.map((source) => (
            <li key={source.id} className="flex items-center gap-1">
              <button
                type="button"
                data-testid={`skill-source-${source.id}`}
                aria-current={!preview && source.id === activeId ? "true" : undefined}
                className={`min-w-0 flex-1 truncate rounded-lg px-2 py-1.5 text-left text-ui hover:bg-muted/60 ${
                  !preview && source.id === activeId ? "bg-muted" : ""
                }`}
                onClick={() => {
                  setPreview(null);
                  refresh.reset();
                  setSelectedId(source.id);
                }}
              >
                {source.name}
                {source.ref && <span className="text-muted-foreground"> @{source.ref}</span>}
              </button>
              {source.managed && (
                <Badge
                  variant="outline"
                  title="Set in the server config"
                  data-testid={`skill-source-managed-${source.id}`}
                >
                  Config
                </Badge>
              )}
              {isAdmin && !source.managed && (
                <DeleteSourceButton id={source.id} name={source.name} />
              )}
            </li>
          ))}
        </ul>
        {sources.hasNextPage && (
          <Button
            variant="link"
            size="sm"
            className="self-start"
            disabled={sources.isFetchingNextPage}
            onClick={() => void sources.fetchNextPage()}
            componentId="settings.skills.sources_more"
          >
            More sources
          </Button>
        )}
        {!sources.isPending && list.length === 0 && (
          <p className="text-ui text-muted-foreground" data-testid="skill-sources-empty">
            {isAdmin
              ? "No sources yet. Add a repository below."
              : "No sources yet. An administrator can add repositories to browse."}
          </p>
        )}
        {isAdmin && <AddSourceForm />}
        {urlInstall && <AddFromUrlForm onPreview={setPreview} />}
      </aside>
      <section>
        {preview && (
          <div className="mb-3 flex items-center justify-between gap-3 text-ui text-muted-foreground">
            <span>
              Previewing <code>{preview.url}</code>
              {preview.ref ? ` @${preview.ref}` : ""}
            </span>
            <Button
              variant="link"
              onClick={() => setPreview(null)}
              componentId="settings.skills.preview_close"
            >
              Back to sources
            </Button>
          </div>
        )}
        {!preview && catalog.isPending && activeId && <Loading label="Fetching the catalog" />}
        {!preview && catalog.isError && <ErrorLine error={catalog.error} />}
        {!preview && refresh.error && <ErrorLine error={refresh.error} />}
        {shown && (
          <CatalogGrid
            // Local install state belongs to one repository: a same-named skill
            // in another catalog is not installed just because this one was.
            key={`${shown.url}@${shown.ref ?? ""}`}
            catalog={shown}
            isAdmin={isAdmin}
            onRefresh={!preview && activeId ? () => refresh.mutate(activeId) : undefined}
            refreshing={refresh.isPending}
          />
        )}
      </section>
    </div>
  );
}

function DeleteSourceButton({ id, name }: { id: string; name: string }) {
  const remove = useDeleteSkillSource();
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Remove source ${name}`}
        disabled={remove.isPending}
        onClick={() => remove.mutate(id)}
        componentId="settings.skills.source_remove"
      >
        <TrashIcon />
      </Button>
      {remove.error && <ErrorLine error={remove.error} />}
    </>
  );
}

function AddSourceForm() {
  const add = useAddSkillSource();
  const [source, setSource] = useState("");
  const [ref, setRef] = useState("");
  return (
    <form
      className="flex flex-col gap-2 rounded-xl border border-border p-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (!source.trim()) return;
        add.mutate(
          { source: source.trim(), ref: ref.trim() || null },
          { onSuccess: () => (setSource(""), setRef("")) },
        );
      }}
    >
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Add a source
      </span>
      <Input
        aria-label="Repository"
        placeholder="owner/repo or github.com URL"
        value={source}
        onChange={(e) => setSource(e.target.value)}
        data-testid="skill-source-input"
      />
      <Input
        aria-label="Branch or tag"
        placeholder="Branch or tag (optional)"
        value={ref}
        onChange={(e) => setRef(e.target.value)}
        data-testid="skill-source-ref"
      />
      {add.error && <ErrorLine error={add.error} />}
      <Button
        type="submit"
        size="sm"
        disabled={add.isPending}
        componentId="settings.skills.source_add"
      >
        <PlusIcon />
        Add source
      </Button>
    </form>
  );
}

function AddFromUrlForm({ onPreview }: { onPreview: (catalog: SkillCatalog) => void }) {
  const preview = usePreviewCatalog();
  const [source, setSource] = useState("");
  const [ref, setRef] = useState("");
  return (
    <form
      className="flex flex-col gap-2 rounded-xl border border-border p-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (!source.trim()) return;
        preview.mutate(
          { source: source.trim(), ref: ref.trim() || null },
          { onSuccess: onPreview },
        );
      }}
    >
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Add from URL
      </span>
      <Input
        aria-label="Repository URL"
        placeholder="owner/repo or github.com URL"
        value={source}
        onChange={(e) => setSource(e.target.value)}
        data-testid="skill-url-input"
      />
      <Input
        aria-label="Branch or tag for the URL"
        placeholder="Branch or tag (optional)"
        value={ref}
        onChange={(e) => setRef(e.target.value)}
        data-testid="skill-url-ref"
      />
      {preview.error && <ErrorLine error={preview.error} />}
      <Button
        type="submit"
        size="sm"
        variant="outline"
        disabled={preview.isPending}
        componentId="settings.skills.preview"
      >
        {preview.isPending ? <Spinner className="size-4" /> : <SearchIcon />}
        Preview skills
      </Button>
    </form>
  );
}

function CatalogGrid({
  catalog,
  isAdmin,
  onRefresh,
  refreshing = false,
}: {
  catalog: SkillCatalog;
  isAdmin: boolean;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  const install = useInstallSkills();
  const [search, setSearch] = useState("");
  const [scope, setScope] = useState<SkillScope>("user");
  // Installs made from this catalog view (a URL preview is local state and
  // never refetched), keyed by name and scope.
  const [justInstalled, setJustInstalled] = useState<Set<string>>(() => new Set());
  const installedIn = (skill: CatalogSkill, target: SkillScope) =>
    justInstalled.has(`${skill.name}:${target}`) ||
    (skill.installed_scopes
      ? skill.installed_scopes.includes(target)
      : skill.installed_id !== null);
  const term = search.trim().toLowerCase();
  const skills = useMemo(
    () =>
      catalog.skills.filter((skill) =>
        `${skill.name} ${skill.display_name ?? ""} ${skill.description} ${skill.plugin ?? ""}`
          .toLowerCase()
          .includes(term),
      ),
    [catalog.skills, term],
  );
  const installOne = (skill: CatalogSkill) =>
    install.mutate(
      { source: catalog.url, ref: catalog.ref, names: [skill.name], scope },
      {
        onSuccess: () => setJustInstalled((prev) => new Set(prev).add(`${skill.name}:${scope}`)),
      },
    );

  return (
    <div className="flex flex-col gap-3" data-testid="skill-catalog">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg border border-border px-2.5">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <input
            type="search"
            aria-label="Search skills"
            placeholder="Search skills…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground/50"
          />
        </div>
        {isAdmin && (
          <label className="flex items-center gap-2 text-ui text-muted-foreground">
            Install for
            <select
              data-testid="install-scope"
              className="rounded-lg border border-border bg-transparent px-2 py-1 text-ui"
              value={scope}
              onChange={(e) => setScope(e.target.value as SkillScope)}
            >
              <option value="user">Only me</option>
              <option value="org">Everyone</option>
            </select>
          </label>
        )}
        {catalog.commit && (
          <span className="text-xs text-muted-foreground">
            at <code>{shortCommit(catalog.commit)}</code>
          </span>
        )}
        {onRefresh && (
          <Button
            variant="ghost"
            size="sm"
            disabled={refreshing}
            onClick={onRefresh}
            componentId="settings.skills.catalog_refresh"
          >
            <RefreshCwIcon className={refreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
        )}
      </div>
      {install.error && <ErrorLine error={install.error} />}
      {skills.length === 0 ? (
        <p className="py-6 text-ui text-muted-foreground" data-testid="skill-catalog-empty">
          {catalog.skills.length === 0
            ? "This repository has no skills in a layout the marketplace reads."
            : "No skills match your search."}
        </p>
      ) : (
        <div className="grid gap-3 @lg:grid-cols-2">
          {skills.map((skill) => (
            <div
              key={`${skill.plugin ?? ""}:${skill.name}`}
              data-testid={`catalog-skill-${skill.name}`}
              className="flex flex-col gap-2 rounded-xl border border-border p-4"
            >
              <span className="flex items-center gap-2 font-medium">
                <PackageIcon className="size-4 shrink-0" aria-hidden />
                <span className="truncate">{skill.display_name ?? skill.name}</span>
                {skill.plugin && <Badge variant="secondary">{skill.plugin}</Badge>}
              </span>
              <span className="text-ui text-muted-foreground">{skill.description}</span>
              <div className="mt-auto flex items-center justify-between gap-2 text-xs text-muted-foreground">
                <code className="truncate">{skill.path}</code>
                {installedIn(skill, scope) ? (
                  <Badge variant="secondary">
                    {scope === "org" ? "Installed for everyone" : "Installed"}
                  </Badge>
                ) : (
                  <Button
                    size="sm"
                    data-testid={`install-skill-${skill.name}`}
                    disabled={install.isPending}
                    onClick={() => installOne(skill)}
                    componentId="settings.skills.install"
                  >
                    Install
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {catalog.skipped.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary>{catalog.skipped.length} entries were skipped</summary>
          <ul className="mt-1 list-disc pl-5">
            {catalog.skipped.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// ── shared bits ───────────────────────────────────────────────────────────

function Loading({ label }: { label: string }) {
  return (
    <span className="flex items-center gap-2 py-4 text-ui text-muted-foreground" role="status">
      <Spinner className="size-4" />
      {label}…
    </span>
  );
}

function ErrorLine({ error }: { error: unknown }) {
  return (
    <p role="alert" className="text-ui text-destructive">
      {errorText(error)}
    </p>
  );
}
