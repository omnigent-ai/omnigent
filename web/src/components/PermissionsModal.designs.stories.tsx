import type { Meta, StoryObj } from "@storybook/react-vite";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { CheckIcon, LinkIcon, LockIcon, QrCodeIcon, Trash2Icon, UsersIcon } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useGrantPermission, usePermissions, useRevokePermission } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { copyText } from "@/lib/clipboard";
import type { OmnigentHostConfig } from "@/lib/host";
import { grantPermission, type Permission } from "@/lib/permissionsApi";
import { updateSession } from "@/lib/sessionsApi";
import { HostConfigSeed, StoryQueryRouter } from "@/storybook/StoryProviders";
import { PermissionsModal } from "./PermissionsModal";

type Design = "current" | "people-first" | "access-first";
type Scenario = "Private" | "People added" | "Link access enabled";

interface SharingDesignProps {
  design: Design;
  scenario: Scenario;
  compare?: boolean;
}

const sessionId = "sharing-design-preview";
const sampleLink = "https://omnigent.example/c/sharing-design-preview";
const serverInfo = {
  ...FALLBACK_SERVER_INFO,
  accounts_enabled: true,
  single_user: false,
  public_sharing_max_level: "read" as const,
};
const designChoices: { design: Design; label: string }[] = [
  { design: "current", label: "Current" },
  { design: "people-first", label: "A · People first" },
  { design: "access-first", label: "B · Access first" },
];

function SharingDesign(props: SharingDesignProps) {
  return <DesignSession key={`${props.design}-${props.scenario}`} {...props} />;
}

function DesignSession({ design, scenario, compare = false }: SharingDesignProps) {
  const [activeDesign, setActiveDesign] = useState(design);
  const [open, setOpen] = useState(!compare);
  const [config] = useState<OmnigentHostConfig>(() => {
    let permissions: Permission[] = [
      { user_id: "you@example.com", conversation_id: sessionId, level: 4 },
    ];
    if (scenario !== "Private") {
      permissions.push(
        { user_id: "alex.morgan@example.com", conversation_id: sessionId, level: 1 },
        { user_id: "sam.taylor@example.com", conversation_id: sessionId, level: 1 },
      );
    }
    if (scenario === "Link access enabled") {
      permissions.push({ user_id: "__public__", conversation_id: sessionId, level: 1 });
    }
    let shareWorkspaceFiles = false;
    const sessionPath = `/v1/sessions/${sessionId}`;
    const permissionsPath = `${sessionPath}/permissions`;

    // Keep the real modal and both explorations on the same in-memory API.
    return {
      transformShareLink: () => sampleLink,
      fetcher: async (path, init) => {
        const pathname = new URL(path, "https://omnigent.example").pathname;
        const method = init?.method ?? "GET";
        if (pathname === "/v1/info") return Response.json(serverInfo);
        if (pathname === "/v1/me") {
          return Response.json({ user_id: "you@example.com", is_admin: true });
        }
        if (pathname === permissionsPath) {
          if (method === "PUT") {
            const body = JSON.parse(String(init?.body)) as { user_id: string; level: number };
            const permission = { ...body, conversation_id: sessionId };
            permissions = [
              ...permissions.filter((entry) => entry.user_id !== body.user_id),
              permission,
            ];
            return Response.json(permission);
          }
          return Response.json({ permissions, next_cursor: null });
        }
        if (pathname.startsWith(`${permissionsPath}/`) && method === "DELETE") {
          const userId = decodeURIComponent(pathname.slice(permissionsPath.length + 1));
          permissions = permissions.filter((entry) => entry.user_id !== userId);
          return new Response(null, { status: 204 });
        }
        if (pathname === sessionPath) {
          if (method === "PATCH") {
            const body = JSON.parse(String(init?.body)) as { share_workspace_files: boolean };
            shareWorkspaceFiles = body.share_workspace_files;
          }
          return Response.json({
            id: sessionId,
            agent_id: "sample-agent",
            status: "idle",
            workspace: "/projects/omnigent",
            permission_level: 4,
            share_workspace_files: shareWorkspaceFiles,
            labels: {},
          });
        }
        return Response.json({ error: { message: "Not part of this preview" } }, { status: 404 });
      },
    };
  });

  return (
    <HostConfigSeed config={config}>
      <CapabilitiesProvider info={serverInfo}>
        <StoryQueryRouter route={`/c/${sessionId}`}>
          <div className="flex min-h-[calc(100vh-3rem)] flex-col items-center justify-center gap-6 bg-background p-6">
            {compare ? (
              <>
                <div className="space-y-2 text-center">
                  <h1 className="text-xl font-semibold">Compare sharing designs</h1>
                  <p className="text-sm text-muted-foreground">
                    Open a design. Close it to try another.
                  </p>
                </div>
                <div className="flex flex-wrap justify-center gap-3">
                  {designChoices.map((choice) => (
                    <Button
                      key={choice.design}
                      variant="outline"
                      size="lg"
                      onClick={() => {
                        setActiveDesign(choice.design);
                        setOpen(true);
                      }}
                    >
                      {choice.label}
                    </Button>
                  ))}
                </div>
              </>
            ) : (
              <Button variant="outline" onClick={() => setOpen(true)}>
                Open share dialog
              </Button>
            )}
          </div>
          {activeDesign !== "people-first" ? (
            <PermissionsModal sessionId={sessionId} open={open} onOpenChange={setOpen} />
          ) : (
            <ShareExploration
              key={activeDesign}
              design={activeDesign}
              open={open}
              onOpenChange={setOpen}
            />
          )}
        </StoryQueryRouter>
      </CapabilitiesProvider>
    </HostConfigSeed>
  );
}

function RoleSelect({
  id,
  value,
  onChange,
  label,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  label: string;
  disabled: boolean;
}) {
  return (
    <Select value={value} onValueChange={onChange} disabled={disabled}>
      <SelectTrigger
        id={id}
        aria-label={label}
        className="w-full min-w-0 sm:w-32 sm:justify-self-end"
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="end">
        <SelectItem value="1">Can view</SelectItem>
        <SelectItem value="2">Can edit</SelectItem>
      </SelectContent>
    </Select>
  );
}

function ShareExploration({
  design,
  open,
  onOpenChange,
}: {
  design: Exclude<Design, "current">;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: permissions = [], isLoading: permissionsLoading } = usePermissions(sessionId);
  const grant = useGrantPermission(sessionId);
  const revoke = useRevokePermission(sessionId);
  const { session } = useSession(sessionId);
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [sharedRole, setSharedRole] = useState(() => {
    const cached = queryClient.getQueryData<Permission[]>(["permissions", sessionId]);
    const permission =
      cached?.find((entry) => entry.user_id === "__public__") ??
      cached?.find((entry) => entry.level < 4);
    return String(permission?.level ?? 1);
  });
  const [copied, setCopied] = useState(false);
  const [showQr, setShowQr] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const filesId = useId();
  const sharingReadOnly =
    serverInfo.sharing_mode === "read_only" || serverInfo.sharing_mode === "restricted_read_only";
  const publicGrant = permissions.find((permission) => permission.user_id === "__public__");
  const people = permissions.filter((permission) => permission.user_id !== "__public__");
  const accessEntries = [
    ...people.filter((permission) => permission.level === 4),
    ...(publicGrant ? [publicGrant] : []),
    ...people.filter((permission) => permission.level !== 4),
  ];
  const reportError = (failure: Error) => setError(failure.message);
  const sharedPermission = useMutation({
    mutationFn: async (nextRole: string) => {
      const results = await Promise.allSettled(
        permissions
          .filter((permission) => permission.level < 4 && permission.level !== Number(nextRole))
          .map((permission) => grantPermission(sessionId, permission.user_id, Number(nextRole))),
      );
      const failure = results.find((result) => result.status === "rejected");
      if (failure) {
        throw failure.reason instanceof Error
          ? failure.reason
          : new Error("Couldn't update access for everyone.");
      }
      return nextRole;
    },
    onSuccess: setSharedRole,
    onError: reportError,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["permissions", sessionId] }),
  });
  const busy =
    permissionsLoading || grant.isPending || revoke.isPending || sharedPermission.isPending;
  const files = useMutation({
    mutationFn: (next: boolean) => updateSession(sessionId, { shareWorkspaceFiles: next }),
    onSuccess: (updated) => queryClient.setQueryData(["session", sessionId], updated),
    onError: reportError,
  });

  function addPerson(event: FormEvent) {
    event.preventDefault();
    const userId = username.trim();
    if (!userId || busy) return;
    setError(null);
    grant.mutate(
      { userId, level: Number(sharedRole) },
      {
        onSuccess: () => {
          setUsername("");
        },
        onError: reportError,
      },
    );
  }

  function changeLinkAccess(access: string) {
    setCopied(false);
    setError(null);
    if (access === "restricted") revoke.mutate("__public__", { onError: reportError });
    else
      grant.mutate({ userId: "__public__", level: Number(sharedRole) }, { onError: reportError });
  }

  async function copyLink() {
    setError(null);
    try {
      await copyText(sampleLink);
      setCopied(true);
    } catch {
      setError("Couldn't copy the link. Try again.");
    }
  }

  const peopleContent = (
    <div className="space-y-3">
      <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
        <UsersIcon className="size-3.5" aria-hidden="true" />
        People with access
      </p>
      {!publicGrant && (
        <form onSubmit={addPerson} className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
          <label htmlFor={inputId} className="sr-only">
            Username or email
          </label>
          <Input
            id={inputId}
            placeholder="Username or email"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            className="min-w-0"
          />
          <Button type="submit" disabled={!username.trim() || busy} loading={grant.isPending}>
            Add
          </Button>
        </form>
      )}

      <div className="space-y-2">
        <ul className="max-h-52 space-y-1 overflow-y-auto" aria-label="People with access">
          {accessEntries.map((permission) => {
            const owner = permission.level === 4;
            const everyone = permission.user_id === "__public__";
            const label = everyone ? "Everyone" : permission.user_id;
            return (
              <li key={permission.user_id} className="flex min-w-0 items-center gap-3 py-2">
                <Avatar>
                  <AvatarFallback>
                    {everyone ? (
                      <UsersIcon className="size-4" aria-hidden="true" />
                    ) : (
                      permission.user_id.slice(0, 1).toUpperCase()
                    )}
                  </AvatarFallback>
                </Avatar>
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium" title={label}>
                    {label}
                  </p>
                </div>
                {owner ? (
                  <span className="px-2 text-sm text-muted-foreground">Owner</span>
                ) : (
                  <>
                    <span className="shrink-0 text-sm text-muted-foreground">
                      {permission.level === 2 ? "Can edit" : "Can view"}
                    </span>
                    {!publicGrant && (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        disabled={busy}
                        className="shrink-0 text-muted-foreground hover:text-destructive"
                        aria-label={`Remove access for ${permission.user_id}`}
                        title="Remove access"
                        onClick={() => {
                          setError(null);
                          revoke.mutate(permission.user_id, { onError: reportError });
                        }}
                      >
                        <Trash2Icon className="size-3.5" aria-hidden="true" />
                      </Button>
                    )}
                  </>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );

  const linkContent = (
    <div className="space-y-4">
      <p className="text-xs leading-relaxed text-muted-foreground">
        {sharingReadOnly
          ? "This Omnigent server allows read-only sharing. Invite others to view or comment on this session."
          : "This Omnigent server allows sharing. Invite others to view or edit this session."}
      </p>
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3">
        <label className="text-sm font-medium" htmlFor={`${inputId}-access`}>
          {design === "people-first" ? "Link access" : "Who can access"}
        </label>
        <Select
          value={publicGrant ? "signed-in" : "restricted"}
          onValueChange={changeLinkAccess}
          disabled={busy}
        >
          <SelectTrigger
            id={`${inputId}-access`}
            className="w-full min-w-0 sm:w-48 sm:justify-self-end"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="end">
            <SelectItem value="restricted">Only added people</SelectItem>
            <SelectItem value="signed-in">Anyone signed in</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3">
        <label className="text-sm font-medium" htmlFor={`${inputId}-role`}>
          What can they do?
        </label>
        <RoleSelect
          id={`${inputId}-role`}
          value={sharedRole}
          label="What can they do?"
          disabled={busy}
          onChange={(value) => {
            setError(null);
            sharedPermission.mutate(value);
          }}
        />
      </div>
    </div>
  );

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[85vh] gap-5 overflow-y-auto sm:max-w-[480px]">
          <DialogHeader>
            <DialogTitle>Share session</DialogTitle>
            <DialogDescription className="sr-only">
              Manage who can open this session and its workspace files.
            </DialogDescription>
          </DialogHeader>

          {design === "people-first" ? (
            <>
              {peopleContent}
              <div className="border-t border-border pt-5">{linkContent}</div>
            </>
          ) : (
            <>
              <div className="border-b border-border pb-5">{linkContent}</div>
              {peopleContent}
            </>
          )}

          <div className="space-y-3 border-t border-border pt-5">
            <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
              <LockIcon className="size-3.5" aria-hidden="true" />
              What they can access
            </p>
            <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-3 py-3">
              <div className="min-w-0 flex-1 space-y-1">
                <label htmlFor={filesId} className="block cursor-pointer text-sm">
                  Viewers can browse workspace files
                </label>
                {session?.shareWorkspaceFiles && (
                  <p
                    id={`${filesId}-description`}
                    className="text-[0.85em] leading-snug text-muted-foreground"
                  >
                    People with Read access can browse files. Comment includes files and commenting;
                    Edit and higher also allow file changes. Existing workspace access still
                    applies.
                  </p>
                )}
              </div>
              <Switch
                id={filesId}
                aria-describedby={
                  session?.shareWorkspaceFiles ? `${filesId}-description` : undefined
                }
                checked={session?.shareWorkspaceFiles ?? false}
                disabled={!session || files.isPending}
                onCheckedChange={(next) => {
                  setError(null);
                  files.mutate(next);
                }}
              />
            </div>
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter className="sm:justify-between">
            <Button variant="ghost" onClick={() => setShowQr(true)}>
              <QrCodeIcon className="size-4" />
              Open in mobile app
            </Button>
            <Button onClick={() => void copyLink()}>
              {copied ? <CheckIcon className="size-4" /> : <LinkIcon className="size-4" />}
              {copied ? "Copied" : "Copy link"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={showQr} onOpenChange={setShowQr}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Open in mobile app</DialogTitle>
            <DialogDescription>Scan this sample session link with your phone.</DialogDescription>
          </DialogHeader>
          <div className="mx-auto rounded-xl bg-white p-4">
            <QRCodeSVG
              value="omnigent://omnigent.example/c/sharing-design-preview"
              size={192}
              level="M"
              bgColor="#ffffff"
              fgColor="#000000"
              aria-label="Sample session QR code"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowQr(false)}>
              Done
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

const meta = {
  title: "Design explorations/Session sharing",
  component: SharingDesign,
  args: { design: "people-first", scenario: "People added" },
  argTypes: {
    design: { control: false },
    compare: { control: false },
    scenario: {
      options: ["Private", "People added", "Link access enabled"],
      control: "inline-radio",
    },
  },
  parameters: { layout: "fullscreen", controls: { include: ["scenario"] } },
} satisfies Meta<typeof SharingDesign>;

export default meta;
type Story = StoryObj<typeof meta>;

export const CompareDesigns: Story = {
  name: "Compare all designs",
  args: { compare: true },
};
export const CurrentPrivate: Story = {
  name: "Current · Private",
  args: { design: "current", scenario: "Private" },
};
export const Current: Story = {
  name: "Current · People added",
  args: { design: "current" },
};
export const CurrentLinkAccess: Story = {
  name: "Current · Link access enabled",
  args: { design: "current", scenario: "Link access enabled" },
};
export const PeopleFirstPrivate: Story = {
  name: "A · People first · Private",
  args: { design: "people-first", scenario: "Private" },
};
export const PeopleFirst: Story = {
  name: "A · People first · People added",
  args: { design: "people-first" },
};
export const PeopleFirstLinkAccess: Story = {
  name: "A · People first · Link access enabled",
  args: { design: "people-first", scenario: "Link access enabled" },
};
export const AccessFirstPrivate: Story = {
  name: "B · Access first · Private",
  args: { design: "access-first", scenario: "Private" },
};
export const AccessFirst: Story = {
  name: "B · Access first · People added",
  args: { design: "access-first" },
};
export const AccessFirstLinkAccess: Story = {
  name: "B · Access first · Link access enabled",
  args: { design: "access-first", scenario: "Link access enabled" },
};
