import { z } from "zod";

export const BrainDocumentV1Schema = z
  .object({
    schema_version: z.literal("brain-document.v1"),
    provider: z.enum(["google", "slack", "notion"]),
    connection_id: z.string().min(1).max(128),
    external_resource_id: z.string().min(1).max(512),
    stable_path: z.string().min(1).max(1024),
    title: z.string().min(1).max(512),
    markdown: z.string(),
    canonical_source_url: z.string().url().startsWith("https://"),
    source_created_at: z.iso.datetime().nullable(),
    source_modified_at: z.iso.datetime(),
    content_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    raw_object_reference: z.string().min(1).max(2048),
    raw_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    transform_schema_version: z.string().min(1).max(64),
    deletion_state: z.enum(["active", "deleted"]),
    visibility_class: z.literal("org-shared"),
  })
  .strict()
  .superRefine((value, context) => {
    const safePath = (path: string, prefix: string, suffix: string) =>
      path.startsWith(prefix) &&
      path.endsWith(suffix) &&
      !path.startsWith("/") &&
      !path.includes("\\") &&
      !path.split("/").includes("..");
    if (!safePath(value.stable_path, `sources/${value.provider}/`, ".md")) {
      context.addIssue({ code: "custom", path: ["stable_path"], message: "Invalid source path" });
    }
    if (!safePath(value.raw_object_reference, `.raw/${value.provider}/`, ".json")) {
      context.addIssue({
        code: "custom",
        path: ["raw_object_reference"],
        message: "Invalid raw object path",
      });
    }
  });

export type BrainDocumentV1 = z.infer<typeof BrainDocumentV1Schema>;
