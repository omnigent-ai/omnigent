/**
 * A short plain-text version of Markdown for one-line previews (board cards): marks,
 * links, code fences and block syntax removed, whitespace collapsed.
 */
export function markdownPlainText(markdown: string): string {
  return (
    markdown
      // Fenced code: keep the code, drop the fences and language tag.
      .replace(/```[^\n]*\n?([\s\S]*?)```/g, "$1")
      // Images keep their alt text, links their label.
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/<([a-z][\w+.-]*:[^>\s]+)>/gi, "$1")
      .replace(/<\/?[a-z][^>]*>/gi, "")
      // Block syntax at line starts: headings, quotes, list markers, rules.
      .replace(/^[ \t]*#{1,6}[ \t]+/gm, "")
      .replace(/^[ \t]*>[ \t]?/gm, "")
      .replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, "")
      .replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, "")
      // Inline code, then emphasis. Underscores only at word edges, so snake_case stays.
      .replace(/`+([^`]*)`+/g, "$1")
      .replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, "$2")
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "$1")
      .replace(/\*(?=\S)([^*]*?\S)\*/g, "$1")
      .replace(/(^|[^\w])_(?=\S)([^_]*?\S)_(?!\w)/g, "$1$2")
      // Escapes, then any stray emphasis markers a clamp would otherwise show.
      .replace(/\\([\\`*_{}[\]()#+\-.!~|>])/g, "$1")
      .replace(/\*{2,}/g, "")
      .replace(/\s+/g, " ")
      .trim()
  );
}
