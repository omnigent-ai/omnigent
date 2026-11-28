// A gadget's data calls answered from memory, so a template can run in its preview with its own
// sample data and nothing is saved.

import { BridgeError } from "./gadgetBridge";
import type { GadgetMethod } from "../sdk/gadgetProtocol";

interface PreviewDocument {
  id: string;
  value: unknown;
  version: string;
  created_at: string;
  updated_at: string;
  created_by: string;
  updated_by: string;
}

export type PreviewRpc = (
  method: GadgetMethod,
  params: Record<string, unknown>,
) => Promise<unknown>;

export function createPreviewRpc(now: () => string = () => new Date().toISOString()): PreviewRpc {
  const collections = new Map<string, Map<string, PreviewDocument>>();
  let counter = 0;
  const copy = (document: PreviewDocument) => structuredClone(document);
  return async (method, params) => {
    const name = String(params.collection);
    const documents = collections.get(name) ?? new Map<string, PreviewDocument>();
    collections.set(name, documents);
    const id = typeof params.id === "string" ? params.id : undefined;
    const existing = id === undefined ? undefined : documents.get(id);
    switch (method) {
      case "data.v1.get":
        return existing ? copy(existing) : null;
      case "data.v1.list": {
        const limit = typeof params.limit === "number" ? params.limit : 100;
        return {
          items: [...documents.values()].slice(0, limit).map(copy),
          next_cursor: null,
        };
      }
      case "data.v1.create": {
        counter += 1;
        const recordId = id ?? `preview-${counter}`;
        if (documents.has(recordId)) {
          throw new BridgeError("CONFLICT", "A record with that ID already exists.");
        }
        const at = now();
        const document = {
          id: recordId,
          value: structuredClone(params.value),
          version: `v${counter}`,
          created_at: at,
          updated_at: at,
          created_by: "preview",
          updated_by: "preview",
        };
        documents.set(recordId, document);
        return copy(document);
      }
      case "data.v1.replace":
      case "data.v1.delete": {
        if (!existing || id === undefined) {
          throw new BridgeError("NOT_FOUND", "The record does not exist.");
        }
        if (existing.version !== params.if_version) {
          throw new BridgeError("CONFLICT", "The record changed since it was read.");
        }
        counter += 1;
        if (method === "data.v1.delete") {
          documents.delete(id);
          return { id, version: `v${counter}`, deleted: true };
        }
        const replaced = {
          ...existing,
          value: structuredClone(params.value),
          version: `v${counter}`,
          updated_at: now(),
        };
        documents.set(id, replaced);
        return copy(replaced);
      }
      default:
        throw new BridgeError("UNSUPPORTED", "Use this template to try that.");
    }
  };
}
