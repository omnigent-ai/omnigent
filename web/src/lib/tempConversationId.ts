import { randomUUID } from "./randomUUID";

export const CLIENT_CREATE_TOKEN_LABEL = "omnigent.client_create_token";
export const TEMP_CONV_ID_PREFIX = "temp:";

export function isTempConvId(id: string | null | undefined): boolean {
  return typeof id === "string" && id.startsWith(TEMP_CONV_ID_PREFIX);
}

export function newTempConversation(): { id: string; token: string } {
  const token = randomUUID().replaceAll("-", "").toLowerCase();
  return { id: `${TEMP_CONV_ID_PREFIX}${token}`, token };
}
