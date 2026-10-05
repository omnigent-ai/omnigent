/** Select the content watermark while accepting rows from older servers. */
export function conversationReadTimestamp(
  updatedAt: number,
  lastMessageAt: number | null | undefined,
): number {
  return lastMessageAt ?? updatedAt;
}
