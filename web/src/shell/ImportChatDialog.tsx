import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2Icon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import {
  importNativeChat,
  listRecentNativeChats,
  type ChatImportSource,
  type RecentNativeChat,
} from "@/lib/chatImports";
import { useNavigate } from "@/lib/routing";
import type { Host } from "@/hooks/useHosts";

export function ImportChatDialog({
  open,
  onOpenChange,
  hosts,
  defaultHostId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  hosts: readonly Host[];
  defaultHostId: string | null;
}) {
  const navigate = useNavigate();
  const onlineHosts = useMemo(() => hosts.filter((host) => host.status === "online"), [hosts]);
  const [source, setSource] = useState<ChatImportSource>("claude");
  const [hostId, setHostId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [recent, setRecent] = useState<RecentNativeChat[] | null>(null);
  const [expandedSessionId, setExpandedSessionId] = useState<string | null>(null);
  const [loadingRecent, setLoadingRecent] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recentRequestRef = useRef(0);

  useEffect(() => {
    if (!open) return;
    setRecent(null);
    setExpandedSessionId(null);
    setSessionId("");
    setError(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setHostId((current) => {
      if (onlineHosts.some((host) => host.host_id === current)) return current;
      if (defaultHostId !== null && onlineHosts.some((host) => host.host_id === defaultHostId)) {
        return defaultHostId;
      }
      return onlineHosts[0]?.host_id ?? "";
    });
  }, [open, onlineHosts, defaultHostId]);

  const loadRecent = useCallback(async () => {
    if (!hostId) return;
    const request = ++recentRequestRef.current;
    setLoadingRecent(true);
    setError(null);
    setRecent(null);
    try {
      const chats = await listRecentNativeChats(hostId, source);
      if (recentRequestRef.current === request) setRecent(chats);
    } catch (caught) {
      if (recentRequestRef.current === request) {
        setError(caught instanceof Error ? caught.message : "Could not load recent chats");
      }
    } finally {
      if (recentRequestRef.current === request) setLoadingRecent(false);
    }
  }, [hostId, source]);

  useEffect(() => {
    if (!open || !hostId) return;
    void loadRecent();
    return () => {
      recentRequestRef.current += 1;
    };
  }, [open, hostId, source, loadRecent]);

  const importChat = async () => {
    const trimmedId = sessionId.trim();
    if (!hostId || !trimmedId) return;
    setImporting(true);
    setError(null);
    try {
      const importedSessionId = await importNativeChat(hostId, source, trimmedId);
      onOpenChange(false);
      navigate(`/c/${importedSessionId}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not import chat");
    } finally {
      setImporting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden sm:max-w-2xl"
        data-testid="import-chat-dialog"
      >
        <DialogHeader>
          <DialogTitle>Import chat</DialogTitle>
          <DialogDescription>
            Load an existing Claude Code or Codex chat from a connected host.
          </DialogDescription>
        </DialogHeader>

        <div className="grid min-h-0 gap-4 overflow-y-auto py-2 pr-1">
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">Model</span>
            <Select
              value={source}
              onValueChange={(value) => {
                setSource(value as ChatImportSource);
                setRecent(null);
                setSessionId("");
                setExpandedSessionId(null);
              }}
            >
              <SelectTrigger data-testid="import-chat-model">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="claude">Claude</SelectItem>
                <SelectItem value="codex">Codex</SelectItem>
              </SelectContent>
            </Select>
          </label>

          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">Host</span>
            <Select
              value={hostId}
              onValueChange={(value) => {
                setHostId(value);
                setRecent(null);
                setSessionId("");
                setExpandedSessionId(null);
              }}
            >
              <SelectTrigger data-testid="import-chat-host">
                <SelectValue placeholder="Select a host" />
              </SelectTrigger>
              <SelectContent>
                {onlineHosts.map((host) => (
                  <SelectItem key={host.host_id} value={host.host_id}>
                    {host.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>

          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">Session ID</span>
            <div className="flex gap-2">
              <Input
                className="min-w-0 flex-1"
                value={sessionId}
                onChange={(event) => setSessionId(event.target.value)}
                placeholder={`${source} session ID`}
                data-testid="import-chat-session-id"
              />
              {sessionId !== "" && (
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label="Clear selected session"
                  onClick={() => setSessionId("")}
                >
                  <XIcon className="size-4" />
                </Button>
              )}
            </div>
          </label>

          {sessionId.trim() === "" && loadingRecent && (
            <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" />
              Loading recent {source} chats…
            </div>
          )}
          {sessionId.trim() === "" && recent !== null && (
            <div
              className="max-h-80 overflow-y-auto rounded-md border"
              data-testid="import-chat-recent"
            >
              {recent.length === 0 ? (
                <p className="p-4 text-sm text-muted-foreground">No recent {source} chats found.</p>
              ) : (
                recent.map((chat) => {
                  const expanded = expandedSessionId === chat.session_id;
                  const canExpand = (chat.title?.length ?? 0) > 80;
                  return (
                    <div key={chat.session_id} className="border-b last:border-b-0">
                      <button
                        type="button"
                        className="flex w-full flex-col gap-0.5 px-3 pt-3 pb-2 text-left hover:bg-muted"
                        onClick={() => {
                          setSessionId(chat.session_id);
                        }}
                      >
                        <span
                          className={`text-sm leading-5 font-medium break-words ${expanded ? "" : "line-clamp-2"}`}
                        >
                          {chat.title || "Untitled chat"}
                        </span>
                        <span className="truncate text-xs text-muted-foreground">
                          {chat.workspace || "No workspace"} · {chat.item_count} items
                        </span>
                        <span className="truncate font-mono text-[11px] text-muted-foreground">
                          {chat.session_id}
                        </span>
                      </button>
                      {canExpand && (
                        <button
                          type="button"
                          className="px-3 pb-2 text-xs font-medium text-muted-foreground hover:text-foreground"
                          onClick={() => setExpandedSessionId(expanded ? null : chat.session_id)}
                        >
                          {expanded ? "Show less" : "Show more"}
                        </button>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          )}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!hostId || !sessionId.trim() || importing}
            onClick={() => void importChat()}
          >
            {importing && <Loader2Icon className="mr-2 size-4 animate-spin" />}
            Import chat
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
