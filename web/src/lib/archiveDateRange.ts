export interface ArchiveDateRange {
  start: Date;
  end: Date;
}

const DATE_PATTERN = /^(\d{4})(\d{2})(\d{2})$/;

function parseDay(value: string): Date | null {
  const match = DATE_PATTERN.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const result = new Date(year, month - 1, day);
  return result.getFullYear() === year &&
    result.getMonth() === month - 1 &&
    result.getDate() === day
    ? result
    : null;
}

export function parseArchiveDateRange(value: string): ArchiveDateRange | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parts = trimmed.split("-");
  if (parts.length > 2) return null;
  const start = parseDay(parts[0]);
  const end = parseDay(parts[1] ?? parts[0]);
  if (!start || !end || start > end) return null;
  return { start, end };
}

export function archiveDateRangeBounds(value: string): { after: number; before: number } | null {
  const range = parseArchiveDateRange(value);
  if (!range) return null;
  const endExclusive = new Date(range.end);
  endExclusive.setDate(endExclusive.getDate() + 1);
  return {
    after: Math.floor(range.start.getTime() / 1000),
    before: Math.floor(endExclusive.getTime() / 1000),
  };
}
