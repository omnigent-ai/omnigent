/** Quote a value so a POSIX shell parses it as one literal argument. */
export function quoteShellArgument(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}
