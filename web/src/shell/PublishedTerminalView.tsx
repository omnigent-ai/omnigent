// The Terminal view where the panes view gives way to it, telling the sidebar which terminals it
// shows, so the sidebar leaves them out and each shell attaches in one place.

import { type ComponentProps, useState } from "react";

import { MainTerminalView } from "./MainTerminalView";
import { usePublishMainTerminalKeys } from "./terminalPlacement";

const NO_KEYS: readonly string[] = [];

export function PublishedTerminalView(
  props: Omit<ComponentProps<typeof MainTerminalView>, "onShownTerminalKeys">,
) {
  const [shownKeys, setShownKeys] = useState<readonly string[]>(NO_KEYS);
  usePublishMainTerminalKeys(props.conversationId, shownKeys);
  return <MainTerminalView {...props} onShownTerminalKeys={setShownKeys} />;
}
