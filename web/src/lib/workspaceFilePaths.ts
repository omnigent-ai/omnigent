/** Drive-absolute or UNC file paths; excludes drive-relative and device paths. */
export function isWindowsFilePath(path: string): boolean {
  if (/^[A-Za-z]:[\\/]/.test(path)) return true;
  const unc = path.match(/^[\\/]{2}([^\\/]+)[\\/]([^\\/]+)(?:[\\/]|$)/);
  return !!unc && ![".", "..", "?"].includes(unc[1]) && ![".", ".."].includes(unc[2]);
}
