// Which changed files are tests or generated, so the review can skip them.
// Test files match glob patterns on the file name (or the path, when a pattern
// has a slash); generated files are recognized by common paths like lockfiles.

/** The default test-file patterns, one glob per line; `{a,b}` lists alternatives. */
export const DEFAULT_TEST_FILE_PATTERNS = `
*Suite.{scala,java}
*Test.{scala,java,kt,swift}
*Tests.{scala,java,kt,swift}
*_test.{go,rs,py,c,cc,cpp,cxx}
*_spec.rb
test_*.py
{test,tests}.py
*.test.{js,jsx,ts,tsx,mjs,cjs,mts,cts}
*.spec.{js,jsx,ts,tsx,mjs,cjs,mts,cts}
tests/*.rs
*/tests/*.rs
`;

/** Why only some generated files can be recognized here. */
export const GENERATED_FILES_NOTICE =
  "Only common generated files, such as lockfiles and minified bundles, are recognized.";

interface CompiledPattern {
  expression: RegExp;
  matchesPath: boolean;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function wildcardExpression(pattern: string): string {
  return escapeRegExp(pattern).replaceAll("\\*", ".*").replaceAll("\\?", ".");
}

function patternExpression(pattern: string): string {
  const alternatives = /\{[^{}]+\}/.exec(pattern);
  if (!alternatives) return wildcardExpression(pattern);
  const values = alternatives[0].slice(1, -1).split(",");
  const prefix = wildcardExpression(pattern.slice(0, alternatives.index));
  const suffix = patternExpression(pattern.slice(alternatives.index + alternatives[0].length));
  return `${prefix}(?:${values.map(wildcardExpression).join("|")})${suffix}`;
}

/** Compile newline-separated globs into a path matcher. Blank lines are ignored. */
export function testFileMatcher(
  configuration: string = DEFAULT_TEST_FILE_PATTERNS,
): (path: string) => boolean {
  const patterns: CompiledPattern[] = configuration
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .map((pattern) => ({
      expression: new RegExp(`^${patternExpression(pattern)}$`),
      matchesPath: pattern.includes("/"),
    }));
  return (path) => {
    const name = path.slice(path.lastIndexOf("/") + 1);
    return patterns.some(({ expression, matchesPath }) =>
      expression.test(matchesPath ? path : name),
    );
  };
}

const defaultTestFileMatcher = testFileMatcher();

/** Whether `path` is a test file under the default patterns. */
export function isTestPath(path: string): boolean {
  return defaultTestFileMatcher(path);
}

/** Whether `path` is a commonly generated file: lockfiles, minified bundles, generated folders. */
export function isGeneratedPath(path: string): boolean {
  const normalized = path.toLowerCase();
  return (
    normalized.includes("/generated/") ||
    normalized.startsWith("generated/") ||
    normalized.endsWith(".generated.swift") ||
    normalized.endsWith(".min.js") ||
    normalized.endsWith(".min.css") ||
    /(^|\/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|cargo\.lock|poetry\.lock)$/.test(
      normalized,
    )
  );
}
