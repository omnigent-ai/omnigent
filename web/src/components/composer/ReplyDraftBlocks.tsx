import { useRef, type ComponentProps } from "react";
import { XIcon } from "lucide-react";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import type { ReplyQuote } from "@/lib/replyDraft";
import { ComposerTextInput } from "./ChatComposer";

type TextInputProps = ComponentProps<typeof ComposerTextInput>;

function ReplyTextInput({ input, keyboard, onGrowth }: TextInputProps & { onGrowth?: () => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useAutoGrowTextarea(ref, String(input.value ?? ""), Infinity, onGrowth);
  return (
    <ComposerTextInput
      keyboard={keyboard}
      input={{ ...input, ref, className: "min-h-[20.8px] max-h-none overflow-y-hidden" }}
    />
  );
}

export function ReplyDraftBlocks({
  quotes,
  keyboard,
  inputFor,
  onRemove,
  onGrowth,
  disabled,
  activeTextId,
}: {
  quotes: ReplyQuote[];
  keyboard: TextInputProps["keyboard"];
  inputFor: (quote: ReplyQuote) => TextInputProps["input"];
  onRemove: (id: string) => void;
  onGrowth?: () => void;
  disabled: boolean;
  activeTextId: string | null;
}) {
  return (
    <div className="flex flex-col gap-1.5 pb-2" data-testid="composer-reply-blocks">
      {quotes.map((quote, index) => (
        <div key={quote.id} className="flex flex-col gap-1.5">
          {(quote.before !== "" || index > 0 || activeTextId === quote.id) && (
            <ReplyTextInput
              keyboard={keyboard}
              onGrowth={onGrowth}
              input={{
                ...inputFor(quote),
                value: quote.before,
                "aria-label": `Reply text before quote ${index + 1}`,
                disabled,
                rows: 1,
              }}
            />
          )}
          <div className="flex items-start gap-2" data-testid="composer-reply-quote">
            <blockquote
              className="min-w-0 flex-1 bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-sm text-muted-foreground"
              title={quote.text}
            >
              <span className="block truncate">
                {quote.text.length > 120 ? `${quote.text.slice(0, 120)}…` : quote.text}
              </span>
            </blockquote>
            <button
              type="button"
              onClick={() => onRemove(quote.id)}
              disabled={disabled}
              className="mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground disabled:opacity-60"
              aria-label="Remove quote"
            >
              <XIcon className="size-3.5" />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
