import { Markdown, MarkdownManager } from "@tiptap/markdown";
import type { MarkdownToken } from "@tiptap/core";

const parsed = new WeakMap<object, { source: string; tokens: MarkdownToken[] }>();

// Keep the existing parser's tokens without changing parsing or serialization.
export const SourceMappedMarkdown = Markdown.extend({
  addOptions() {
    const options = this.parent?.();
    const base = options?.marked ?? new MarkdownManager().instance;
    let marked: typeof base;
    class SourceLexer extends base.Lexer {
      override lex(source: string) {
        const tokens = super.lex(source);
        parsed.set(marked, { source: source.replace(/\r\n?/g, "\n"), tokens });
        return tokens;
      }
    }
    marked = new Proxy(base, {
      get(target, key, receiver) {
        return key === "Lexer" ? SourceLexer : Reflect.get(target, key, receiver);
      },
    });
    return { ...options, marked };
  },
});

export function markdownTokens(manager: MarkdownManager, source: string): MarkdownToken[] {
  const marked = manager.instance;
  const previous = parsed.get(marked);
  return previous?.source === source
    ? previous.tokens
    : new marked.Lexer(marked.defaults).lex(source);
}
