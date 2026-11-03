export {};

declare global {
  var omnigentUrl: {
    normalizeUrl: (raw: string) => string;
    isPlainHttpRemote: (raw: string) => boolean;
  };
}
