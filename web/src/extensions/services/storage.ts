export const EXTENSION_STORAGE_DATABASE = "omnigent-extensions";
export const EXTENSION_STORAGE_STORE = "values";
export const EXTENSION_STORAGE_MAX_BYTES = 256 * 1024;
export const EXTENSION_STORAGE_MAX_VALUE_BYTES = 32 * 1024;
export const EXTENSION_STORAGE_MAX_KEYS = 128;
export const EXTENSION_STORAGE_WRITE_INTERVAL_MS = 25;

interface StoredValue {
  id: string;
  namespace: string;
  key: string;
  json: string;
  size: number;
}

export class ExtensionStorageError extends Error {
  readonly code: "InvalidKey" | "InvalidValue" | "QuotaExceeded" | "Unavailable";

  constructor(
    code: "InvalidKey" | "InvalidValue" | "QuotaExceeded" | "Unavailable",
    message: string,
  ) {
    super(message);
    this.code = code;
    this.name = "ExtensionStorageError";
  }
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB aborted"));
  });
}

let databasePromise: Promise<IDBDatabase> | null = null;

function database(): Promise<IDBDatabase> {
  if (!globalThis.indexedDB) {
    return Promise.reject(new ExtensionStorageError("Unavailable", "IndexedDB is unavailable"));
  }
  databasePromise ??= new Promise((resolve, reject) => {
    const request = indexedDB.open(EXTENSION_STORAGE_DATABASE, 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(EXTENSION_STORAGE_STORE, { keyPath: "id" });
      store.createIndex("namespace", "namespace", { unique: false });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Could not open extension storage"));
  });
  return databasePromise;
}

function validateKey(key: unknown): asserts key is string {
  if (
    typeof key !== "string" ||
    key.length < 1 ||
    key.length > 128 ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(key)
  ) {
    throw new ExtensionStorageError("InvalidKey", "Storage key is invalid");
  }
}

function serializeValue(value: unknown): { json: string; size: number } {
  let json: string | undefined;
  try {
    json = JSON.stringify(value);
  } catch {
    throw new ExtensionStorageError("InvalidValue", "Storage value must be JSON serializable");
  }
  if (json === undefined) {
    throw new ExtensionStorageError("InvalidValue", "Storage value must be JSON serializable");
  }
  const size = new TextEncoder().encode(json).byteLength;
  if (size > EXTENSION_STORAGE_MAX_VALUE_BYTES) {
    throw new ExtensionStorageError("QuotaExceeded", "Storage value exceeds 32 KB");
  }
  return { json, size };
}

function namespace(serverIdentity: string, userId: string, extensionId: string): string {
  return `v1/${encodeURIComponent(serverIdentity)}/${encodeURIComponent(userId)}/${encodeURIComponent(extensionId)}`;
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException("Storage operation cancelled", "AbortError");
}

export class ExtensionStorageWriteLimiter {
  private tail: Promise<void> = Promise.resolve();
  private lastWriteAt = 0;

  run<T>(signal: AbortSignal, operation: () => Promise<T>): Promise<T> {
    const execute = async () => {
      throwIfAborted(signal);
      const waitMs = Math.max(
        0,
        EXTENSION_STORAGE_WRITE_INTERVAL_MS - (Date.now() - this.lastWriteAt),
      );
      if (waitMs > 0) {
        await new Promise<void>((resolve) => {
          setTimeout(resolve, waitMs);
        });
      }
      throwIfAborted(signal);
      const result = await operation();
      this.lastWriteAt = Date.now();
      return result;
    };
    const result = this.tail.then(execute, execute);
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

export class ExtensionUserStorage {
  readonly namespace: string;

  constructor(serverIdentity: string, userId: string, extensionId: string) {
    this.namespace = namespace(serverIdentity, userId, extensionId);
  }

  private id(key: string): string {
    return `${this.namespace}/${encodeURIComponent(key)}`;
  }

  async get(key: unknown, signal?: AbortSignal): Promise<unknown> {
    validateKey(key);
    throwIfAborted(signal);
    const db = await database();
    throwIfAborted(signal);
    const transaction = db.transaction(EXTENSION_STORAGE_STORE, "readonly");
    const record = await requestResult<StoredValue | undefined>(
      transaction.objectStore(EXTENSION_STORAGE_STORE).get(this.id(key)),
    );
    throwIfAborted(signal);
    return record ? JSON.parse(record.json) : null;
  }

  async set(key: unknown, value: unknown, signal?: AbortSignal): Promise<void> {
    validateKey(key);
    throwIfAborted(signal);
    const serialized = serializeValue(value);
    const db = await database();
    throwIfAborted(signal);
    const transaction = db.transaction(EXTENSION_STORAGE_STORE, "readwrite");
    const store = transaction.objectStore(EXTENSION_STORAGE_STORE);
    const existing = await requestResult<StoredValue[]>(
      store.index("namespace").getAll(this.namespace),
    );
    throwIfAborted(signal);
    const previous = existing.find((record) => record.key === key);
    const nextBytes =
      existing.reduce((total, record) => total + record.size, 0) -
      (previous?.size ?? 0) +
      serialized.size;
    const nextKeys = existing.length + (previous ? 0 : 1);
    if (nextBytes > EXTENSION_STORAGE_MAX_BYTES || nextKeys > EXTENSION_STORAGE_MAX_KEYS) {
      transaction.abort();
      throw new ExtensionStorageError("QuotaExceeded", "Extension storage quota exceeded");
    }
    store.put({
      id: this.id(key),
      namespace: this.namespace,
      key,
      json: serialized.json,
      size: serialized.size,
    } satisfies StoredValue);
    await transactionDone(transaction);
  }

  async delete(key: unknown, signal?: AbortSignal): Promise<void> {
    validateKey(key);
    throwIfAborted(signal);
    const db = await database();
    throwIfAborted(signal);
    const transaction = db.transaction(EXTENSION_STORAGE_STORE, "readwrite");
    transaction.objectStore(EXTENSION_STORAGE_STORE).delete(this.id(key));
    await transactionDone(transaction);
  }
}

export async function resetExtensionStorageForTests(): Promise<void> {
  if (databasePromise) (await databasePromise).close();
  databasePromise = null;
  if (!globalThis.indexedDB) return;
  await requestResult(indexedDB.deleteDatabase(EXTENSION_STORAGE_DATABASE));
}
