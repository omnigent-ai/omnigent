// Chat-header button that opens the session's workspace in a desktop code
// editor, using the links the server put on the session. Renders nothing when
// the server offers none this client can use.

import { ChevronDownIcon } from "lucide-react";
import { useState, type ComponentType } from "react";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { VSCodeIcon } from "@/components/icons/VSCodeIcon";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSession } from "@/hooks/useSession";
import {
  defaultEditorLink,
  launchEditorLink,
  readDefaultCodeEditor,
  usableEditorLinks,
  writeDefaultCodeEditor,
} from "@/lib/editorLinks";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import type { EditorLink } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useDesktopHostId } from "./RevealInFileManager";
import {
  SEGMENT_ACTIVE,
  SEGMENT_BASE,
  SEGMENT_INACTIVE,
  SEGMENTED_TRACK,
} from "./segmentedControl";

const EDITOR_ICONS: Record<string, ComponentType<{ size?: number | string }>> = {
  cursor: CursorIcon,
  vscode: VSCodeIcon,
};

// Hidden from assistive tech: the label next to it already names the editor, and
// some brand glyphs carry their own <title>, which would read it twice.
function EditorIcon({ editor }: { editor: string }) {
  const Icon = EDITOR_ICONS[editor];
  return (
    <span aria-hidden="true" className="inline-flex">
      {Icon && <Icon size={14} />}
    </span>
  );
}

/**
 * Open-in-editor split button: the main half opens the default editor (shown
 * by its logo and name), the chevron lists the editors with the default checked.
 */
export function OpenInEditorButton({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const desktopHostId = useDesktopHostId();
  const [preferred, setPreferred] = useState(readDefaultCodeEditor);

  const links = usableEditorLinks(session?.editorLinks, session?.hostId, desktopHostId);
  const current = defaultEditorLink(links, preferred);
  // Phone shells have no desktop editor to hand the link to.
  if (!current || isIOSShell() || isAndroidShell()) return null;

  const open = (link: EditorLink) => {
    writeDefaultCodeEditor(link.editor);
    setPreferred(link.editor);
    launchEditorLink(link.url);
  };
  const label = `Open in ${current.label}`;

  return (
    <div className={SEGMENTED_TRACK} data-testid="open-in-editor">
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            aria-label={label}
            onClick={() => open(current)}
            componentId="chat.header.open_in_editor"
            className={cn(SEGMENT_BASE, SEGMENT_ACTIVE, "gap-1.5 px-2")}
          >
            <EditorIcon editor={current.editor} />
            <span className="hidden lg:inline">{current.label}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{label}</TooltipContent>
      </Tooltip>
      {links.length > 1 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Choose code editor"
              componentId="chat.header.choose_code_editor"
              className={cn(SEGMENT_BASE, SEGMENT_INACTIVE, "w-5 px-0")}
            >
              <ChevronDownIcon className="size-3" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-36">
            {/* Picking an editor makes it the default and opens it, like OpenUI. */}
            <DropdownMenuRadioGroup value={current.editor}>
              {links.map((link) => (
                <DropdownMenuRadioItem
                  key={link.editor}
                  value={link.editor}
                  onSelect={() => open(link)}
                >
                  <EditorIcon editor={link.editor} />
                  {link.label}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
