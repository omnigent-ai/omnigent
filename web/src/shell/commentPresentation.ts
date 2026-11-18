export function avatarStyle(name: string): { backgroundColor: string; color: string } {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return { backgroundColor: `hsl(${hash % 360} 60% 50%)`, color: "white" };
}

export function formatCommentTime(createdAt: number, now: Date): string {
  const date = new Date(createdAt * 1000);
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return `${time} Today`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `${time} Yesterday`;
  return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
}
