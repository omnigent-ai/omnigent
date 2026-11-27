// The icon each session action shows in ⌘K, as the row menu shows it.

import {
  AppWindowIcon,
  ArchiveIcon,
  ArchiveRestoreIcon,
  BellIcon,
  BellOffIcon,
  CircleStopIcon,
  ClockPlusIcon,
  FolderInputIcon,
  GitForkIcon,
  ListChevronsUpDownIcon,
  type LucideIcon,
  MailIcon,
  MailOpenIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  ShareIcon,
  Trash2Icon,
} from "lucide-react";

import type { SessionCommandId } from "./model";

export const SESSION_COMMAND_ICONS: Record<SessionCommandId, LucideIcon> = {
  "add-sibling": PlusIcon,
  "open-in-new-window": AppWindowIcon,
  pin: PinIcon,
  unpin: PinOffIcon,
  share: ShareIcon,
  fork: GitForkIcon,
  rename: PencilIcon,
  mute: BellOffIcon,
  unmute: BellIcon,
  "mark-unread": MailIcon,
  "mark-read": MailOpenIcon,
  "create-automation": ClockPlusIcon,
  "move-to-project": FolderInputIcon,
  // The row menu's Nests submenu.
  "move-to-nest": ListChevronsUpDownIcon,
  stop: CircleStopIcon,
  archive: ArchiveIcon,
  unarchive: ArchiveRestoreIcon,
  delete: Trash2Icon,
  // The row menu's Leave takes Delete's slot and its trash icon.
  leave: Trash2Icon,
};
