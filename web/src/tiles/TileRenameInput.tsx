// Inline rename for a tile, reusing the header's input and rename mutation.

import { useRenameConversation } from "@/hooks/useConversations";
import { HeaderTitleInput } from "@/shell/HeaderTitle";

export function TileRenameInput({
  sessionId,
  title,
  onDone,
}: {
  sessionId: string;
  title: string;
  onDone: () => void;
}) {
  const rename = useRenameConversation();
  return (
    <HeaderTitleInput
      initialTitle={title}
      pending={rename.isPending}
      onCommit={(next) => {
        const trimmed = next.trim();
        if (trimmed && trimmed !== title) rename.mutate({ id: sessionId, title: trimmed });
        onDone();
      }}
      onCancel={onDone}
    />
  );
}
