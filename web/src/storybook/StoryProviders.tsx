import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { useChatStore } from "@/store/chatStore";

type ChatStoreState = ReturnType<typeof useChatStore.getState>;

export function ChatStoreSeed({
  seed,
  children,
}: {
  seed: Partial<ChatStoreState>;
  children: ReactNode;
}) {
  const [previous] = useState(() => {
    const current = useChatStore.getState();
    const snapshot: Partial<ChatStoreState> = {};
    for (const key of Object.keys(seed) as (keyof ChatStoreState)[]) {
      Object.assign(snapshot, { [key]: current[key] });
    }
    useChatStore.setState(seed);
    return snapshot;
  });
  useEffect(() => () => useChatStore.setState(previous), [previous]);
  return children;
}

export function StoryQueryRouter({
  children,
  route = "/",
  seed,
}: {
  children: ReactNode;
  route?: string;
  seed?: (queryClient: QueryClient) => void;
}) {
  const [queryClient] = useState(() => {
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false, refetchOnWindowFocus: false, staleTime: Infinity },
      },
    });
    seed?.(client);
    return client;
  });

  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}
