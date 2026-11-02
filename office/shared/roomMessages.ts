// Messages the PO sends from a room, kept so the room's channel shows them.

export interface RoomMessage {
  id: string;
  at: number;
  text: string;
  /** Who it reached, e.g. "@PM" or "QA · SDLC". */
  recipients: { sessionId: string; name: string }[];
}

/** A channel keeps this many of the PO's latest messages. */
export const MAX_ROOM_MESSAGES = 200;
