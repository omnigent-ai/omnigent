import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { CopyIcon, FolderOpenIcon } from "lucide-react";
import { toast } from "sonner";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { copyText } from "@/lib/clipboard";
import {
  getHostIdentity,
  isMacElectronShell,
  revealFile,
  supportsFileReveal,
} from "@/lib/nativeBridge";

const FileMenuContext = createContext<{ root: string | null; localHostId: string | null }>({
  root: null,
  localHostId: null,
});

export function FileMenuProvider({
  root,
  hostId,
  children,
}: {
  root: string | null;
  hostId: string | null;
  children: ReactNode;
}) {
  const [machineHostId, setMachineHostId] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    if (supportsFileReveal()) {
      void getHostIdentity().then((identity) => {
        if (active) setMachineHostId(identity?.hostId ?? null);
      });
    }
    return () => {
      active = false;
    };
  }, []);
  const value = useMemo(
    () => ({ root, localHostId: hostId && hostId === machineHostId ? hostId : null }),
    [root, hostId, machineHostId],
  );
  return <FileMenuContext.Provider value={value}>{children}</FileMenuContext.Provider>;
}

/** Shared by row/tab context menus and the viewer's toolbar menu. */
export function useFileMenuActions(path: string, deleted = false) {
  const { root, localHostId } = useContext(FileMenuContext);
  const absolutePath = resolveFileMenuPath(root, path);
  const relativePath = relativeFileMenuPath(root, path);
  async function copy(value: string) {
    try {
      await copyText(value);
      toast.success("Path copied");
    } catch {
      toast.error("Failed to copy path");
    }
  }
  const actions = [];
  if (localHostId && absolutePath && !deleted) {
    actions.push({
      key: "reveal-file",
      label: isMacElectronShell()
        ? "Show in Finder"
        : navigator.userAgent.includes("Windows")
          ? "Show in File Explorer"
          : "Show in File Manager",
      icon: <FolderOpenIcon className="size-4" />,
      active: false,
      disabled: false,
      onSelect: () => {
        void revealFile(localHostId, absolutePath).then((ok) => {
          if (!ok) toast.error("Could not show this item in the file manager");
        });
      },
    });
  }
  actions.push({
    key: "copy-path",
    label: "Copy Path",
    icon: <CopyIcon className="size-4" />,
    active: false,
    disabled: !absolutePath,
    onSelect: () => {
      if (absolutePath) void copy(absolutePath);
    },
  });
  if (relativePath !== null) {
    actions.push({
      key: "copy-relative-path",
      label: "Copy Relative Path",
      icon: <CopyIcon className="size-4" />,
      active: false,
      disabled: false,
      onSelect: () => {
        void copy(relativePath);
      },
    });
  }
  return actions;
}

export function FileContextMenu({
  path,
  deleted = false,
  children,
}: {
  path: string;
  deleted?: boolean;
  children: ReactElement;
}) {
  const actions = useFileMenuActions(path, deleted);
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        {actions.map((action) => (
          <ContextMenuItem key={action.key} disabled={action.disabled} onSelect={action.onSelect}>
            {action.icon}
            {action.label}
          </ContextMenuItem>
        ))}
      </ContextMenuContent>
    </ContextMenu>
  );
}

function isAbsoluteFilePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[/\\]/.test(path) || path.startsWith("\\\\");
}

/** Directory rows are relative; viewer tabs can also address files outside the workspace. */
export function resolveFileMenuPath(root: string | null, path: string): string | null {
  if (isAbsoluteFilePath(path)) return path;
  if (!root) return null;
  const windows = /^[A-Za-z]:[/\\]/.test(root) || root.startsWith("\\\\");
  const separator = windows ? "\\" : "/";
  const normalizedRoot = windows ? root.replace(/\//g, "\\") : root;
  const base = normalizedRoot.replace(/[/\\]+$/, "");
  return `${base}${separator}${windows ? path.replace(/\//g, "\\") : path}`;
}

/** Do not label an outside-workspace absolute path as a relative path. */
function relativeFileMenuPath(root: string | null, path: string): string | null {
  if (!isAbsoluteFilePath(path)) return path;
  if (!root) return null;
  const windows = /^[A-Za-z]:[/\\]/.test(root) || root.startsWith("\\\\");
  const normalize = (value: string) => (windows ? value.replace(/\\/g, "/") : value);
  const prefix = `${normalize(root).replace(/\/+$/, "")}/`;
  const normalized = normalize(path);
  const matches = windows
    ? normalized.toLowerCase().startsWith(prefix.toLowerCase())
    : normalized.startsWith(prefix);
  return matches ? normalized.slice(prefix.length) : null;
}
