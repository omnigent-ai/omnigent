// Collapsed aside for a `/btw` side question and its answer.
//
// Deliberately quiet and collapsed by default: the exchange is in the
// transcript but was never in the model's context, so it must not read
// like a turn the agent acted on. The trigger row shows the question;
// expanding reveals the answer.

import { ChevronRightIcon, MessageCircleQuestionIcon } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

interface SideQuestionCardProps {
  /** Verbatim text the user typed after `/btw`. */
  question: string;
  /** The harness's reply. */
  answer: string;
}

export function SideQuestionCard({ question, answer }: SideQuestionCardProps) {
  return (
    <Collapsible defaultOpen={false} className="group not-prose w-full">
      <CollapsibleTrigger className="w-full cursor-pointer">
        <span
          title={`btw ${question}`}
          className="flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-sm transition-colors hover:text-foreground"
          data-testid="side-question-card"
        >
          <MessageCircleQuestionIcon className="size-3 shrink-0 text-slate-500 dark:text-slate-400" />
          <span className="min-w-0 flex-1 truncate">
            <span className="font-semibold text-foreground">btw</span>{" "}
            <span className="text-muted-foreground/80">{question}</span>
          </span>
          <ChevronRightIcon className="size-3 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent
        className="mt-1 ml-2 space-y-2 border-l py-1 pl-3 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in"
        data-testid="side-question-answer"
      >
        <FilePathAwareMessageResponse>{answer}</FilePathAwareMessageResponse>
        <p className="text-muted-foreground/70 text-xs">
          Asked with <code>/btw</code> — not part of the conversation's context.
        </p>
      </CollapsibleContent>
    </Collapsible>
  );
}
