// Per-device choice for how messages other sessions send with `sys_session_message`
// appear in a chat. Supervisor sessions can receive many; collapsing them keeps the
// human's own messages and the agent's replies readable. Agents see the full text.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const PEER_SESSION_MESSAGES_STORAGE_KEY = "omnigent:peer-session-messages";

export const peerSessionMessageDisplays = ["collapse", "show", "hide"] as const;
export type PeerSessionMessageDisplay = (typeof peerSessionMessageDisplays)[number];

export const DEFAULT_PEER_SESSION_MESSAGE_DISPLAY: PeerSessionMessageDisplay = "collapse";

const preference = createStoredChoicePreference(
  PEER_SESSION_MESSAGES_STORAGE_KEY,
  peerSessionMessageDisplays,
  DEFAULT_PEER_SESSION_MESSAGE_DISPLAY,
);

/** Read the preference; storage errors and unknown values mean the default. */
export const readPeerSessionMessageDisplay = preference.read;

/** Persist the preference and notify chats already on screen. */
export const writePeerSessionMessageDisplay = preference.write;

/** Re-read after a same-tab write that bypassed `writePeerSessionMessageDisplay` (settings import). */
export const notifyPeerSessionMessageDisplayChanged = preference.notifyChanged;

/** Live value of the preference, including changes made in other windows. */
export const usePeerSessionMessageDisplay = preference.use;
