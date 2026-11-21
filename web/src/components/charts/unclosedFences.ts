// Marks code blocks whose fence never closed, so a chart draws only after its
// closing fence. Streamdown reports an open fence only while text streams, so
// the Markdown parser's own verdict is recorded instead.

interface MdastNode {
  type: string;
  data?: Record<string, unknown>;
  children?: MdastNode[];
  position?: { start: { offset?: number } };
}

interface HastNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  position?: { start: { offset?: number } };
}

interface ParseContext {
  stack: MdastNode[];
}

interface MarkdownFile {
  data: Record<string, unknown>;
}

/** Prop a code block's `pre` receives when its fence never closed. */
export const UNCLOSED_FENCE_PROP = "data-unclosed-fence";

// Where, in the parsed text, each fenced code block that never closed starts.
const UNCLOSED_FENCE_STARTS = "omnigentUnclosedFenceStarts";

/**
 * Parser extension that counts each fenced code block's fences: the opening
 * fence, and the closing one when the parser found it.
 */
const COUNT_FENCES = {
  enter: {
    codeFencedFence(this: ParseContext) {
      for (let index = this.stack.length - 1; index >= 0; index -= 1) {
        const node = this.stack[index];
        if (node.type !== "code") continue;
        node.data = { ...node.data, fences: Number(node.data?.fences ?? 0) + 1 };
        return;
      }
    },
  },
};

/**
 * Remark plugin that records where each fenced code block whose closing fence
 * the parser never found starts, for {@link rehypeMarkUnclosedFences}. It takes
 * CommonMark's verdict, so list items, blockquotes, tabs and indentation follow
 * the parser exactly.
 */
export function remarkFindUnclosedFences(this: { data: () => object }) {
  const data = this.data() as Record<string, unknown>;
  const extensions = (data.fromMarkdownExtensions ??= []) as unknown[];
  extensions.push(COUNT_FENCES);
  return (tree: MdastNode, file: MarkdownFile) => {
    const starts = new Set<number>();
    const pending = [tree];
    while (pending.length > 0) {
      const node = pending.pop()!;
      const start = node.position?.start.offset;
      // Indented code has no fences; a fenced block that never closed has one.
      if (node.type === "code" && node.data?.fences === 1 && start !== undefined) {
        starts.add(start);
      }
      for (const child of node.children ?? []) pending.push(child);
    }
    file.data[UNCLOSED_FENCE_STARTS] = starts;
  };
}

/**
 * Rehype plugin that sets {@link UNCLOSED_FENCE_PROP} on each `pre` whose fence
 * {@link remarkFindUnclosedFences} found unclosed. It runs after sanitizing, so
 * the property reaches the code block component.
 */
export function rehypeMarkUnclosedFences() {
  return (tree: HastNode, file: MarkdownFile) => {
    const starts = file.data[UNCLOSED_FENCE_STARTS];
    if (!(starts instanceof Set) || starts.size === 0) return;
    const pending = [tree];
    while (pending.length > 0) {
      const node = pending.pop()!;
      if (node.tagName === "pre") {
        if (starts.has(node.position?.start.offset)) {
          node.properties = { ...node.properties, dataUnclosedFence: true };
        }
        continue;
      }
      for (const child of node.children ?? []) pending.push(child);
    }
  };
}
