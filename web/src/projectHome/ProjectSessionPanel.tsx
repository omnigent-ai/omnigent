import { useEffect } from "react";
import { useNavigate } from "@/lib/routing";
import { getSessionSharePath } from "@/lib/shareLink";

/** Keep saved project-panel links working without a second session view. */
export function ProjectSessionPanel({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  useEffect(() => {
    void navigate(getSessionSharePath(encodeURIComponent(sessionId)), { replace: true });
  }, [navigate, sessionId]);
  return null;
}
