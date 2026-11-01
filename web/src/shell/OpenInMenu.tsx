import { useSyncExternalStore, type ReactNode } from "react";
import { ChevronDownIcon, CheckIcon, FolderOpenIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { VSCodeIcon } from "@/components/icons/VSCodeIcon";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useHostSshTarget } from "@/hooks/useHostSshTarget";
import { CODE_EDITORS, editorLink, launchEditorLink } from "@/lib/editorLinks";
import { isAndroidShell, isIOSShell, supportsFileReveal } from "@/lib/nativeBridge";
import { useFileViewerConversationId } from "./FileViewerContext";
import {
  revealInFileManager,
  revealLabel,
  useHostFileTarget,
  useLocalFileTarget,
} from "./RevealInFileManager";

const APP_PREFERENCE_EVENT = "omnigent:open-in-app-changed";
const PREFERENCE_KEY = "omnigent:default-code-editor";

function readPreferredApp(): string {
  try {
    return localStorage.getItem(PREFERENCE_KEY) ?? "";
  } catch {
    return "";
  }
}

function rememberApp(appId: string) {
  try {
    localStorage.setItem(PREFERENCE_KEY, appId);
  } catch {
    // Opening still works if this browser cannot save preferences.
  }
  window.dispatchEvent(new Event(APP_PREFERENCE_EVENT));
}

function subscribeToAppPreference(listener: () => void) {
  window.addEventListener(APP_PREFERENCE_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(APP_PREFERENCE_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

interface OpenInAction {
  key: string;
  label: string;
  icon: ReactNode;
  active: boolean;
  onSelect: () => void;
}

/** Shared actions for the folder header and the file viewer's responsive toolbar. */
export function useOpenInActions(path: string | null, directory = false): OpenInAction[] {
  const localTarget = useLocalFileTarget(path);
  const target = useHostFileTarget(path);
  const conversationId = useFileViewerConversationId();
  const mobile = isAndroidShell() || isIOSShell();
  const sshTarget = useHostSshTarget(
    !mobile && !localTarget ? (target?.hostId ?? null) : null,
    conversationId,
  );
  const preferred = useSyncExternalStore(subscribeToAppPreference, readPreferredApp, () => "");
  if (mobile || !target || (!localTarget && !sshTarget)) return [];
  const actions: OpenInAction[] = [];
  for (const editor of CODE_EDITORS) {
    const url = editorLink(editor.id, target.path, localTarget ? null : sshTarget, directory);
    if (!url) continue;
    actions.push({
      key: editor.id,
      label: editor.label,
      icon: (
        <span aria-hidden="true" className="inline-flex size-4 shrink-0">
          {editor.id === "cursor" ? <CursorIcon size={16} /> : <VSCodeIcon size={16} />}
        </span>
      ),
      active: false,
      onSelect: () => {
        rememberApp(editor.id);
        launchEditorLink(url);
      },
    });
  }
  if (localTarget && supportsFileReveal()) {
    actions.push({
      key: "reveal",
      label: revealLabel(directory),
      icon: <FolderOpenIcon className="size-4" />,
      active: false,
      onSelect: () => revealInFileManager(localTarget),
    });
  }
  const defaultAction = actions.find((action) => action.key === preferred) ?? actions[0];
  for (const action of actions) action.active = action === defaultAction;
  return actions;
}

export function OpenInSplitButton({
  actions,
  interactive = true,
}: {
  actions: OpenInAction[];
  interactive?: boolean;
}) {
  const defaultAction = actions.find((action) => action.active) ?? actions[0];
  if (!defaultAction) return null;
  const label =
    defaultAction.key === "reveal" ? defaultAction.label : `Open in ${defaultAction.label}`;
  return (
    <DropdownMenu>
      <div role="group" aria-label="Open in" className="inline-flex shrink-0">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="rounded-r-none border-r-0"
                aria-label={label}
                tabIndex={interactive ? undefined : -1}
                onClick={interactive ? defaultAction.onSelect : undefined}
              >
                {defaultAction.icon}
                Open
              </Button>
            </TooltipTrigger>
            <TooltipContent>{label}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            className="w-6 rounded-l-none"
            aria-label="Choose app to open in"
            tabIndex={interactive ? undefined : -1}
          >
            <ChevronDownIcon />
          </Button>
        </DropdownMenuTrigger>
      </div>
      <DropdownMenuContent align="end">
        {actions.map((action) => (
          <DropdownMenuItem key={action.key} onSelect={interactive ? action.onSelect : undefined}>
            {action.icon}
            {action.label}
            {action.active && <CheckIcon className="ml-auto size-4" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function OpenInMenu({ path, directory = false }: { path: string; directory?: boolean }) {
  const actions = useOpenInActions(path, directory);
  return <OpenInSplitButton actions={actions} />;
}
