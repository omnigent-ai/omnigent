// js-yaml ships no type declarations; this declares only what the shell calls.
declare module "js-yaml" {
  /** Parse one YAML document; the result is untrusted data. */
  export function load(input: string): unknown;
}
