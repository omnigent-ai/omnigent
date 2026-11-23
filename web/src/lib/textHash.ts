/** A 53-bit string hash (cyrb53): synchronous, and the same in every browser. */
export function hashText(text: string): string {
  let hashA = 0xdeadbeef;
  let hashB = 0x41c6ce57;
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index);
    hashA = Math.imul(hashA ^ code, 2654435761);
    hashB = Math.imul(hashB ^ code, 1597334677);
  }
  hashA =
    Math.imul(hashA ^ (hashA >>> 16), 2246822507) ^ Math.imul(hashB ^ (hashB >>> 13), 3266489909);
  hashB =
    Math.imul(hashB ^ (hashB >>> 16), 2246822507) ^ Math.imul(hashA ^ (hashA >>> 13), 3266489909);
  return (4294967296 * (2097151 & hashB) + (hashA >>> 0)).toString(36);
}
