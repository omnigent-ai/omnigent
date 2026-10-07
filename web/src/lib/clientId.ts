import { randomUUID } from "@/lib/randomUUID";

/**
 * Identity of this SPA instance (one per page load, so each tab and the desktop
 * shell is a distinct client), sent with the session stream and queue share.
 */
export const CLIENT_ID = `c_${randomUUID().replace(/-/g, "")}`;
