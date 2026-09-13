export class ExtensionHostServiceError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
    this.name = "ExtensionHostServiceError";
  }
}
