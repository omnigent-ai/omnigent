import { spawn } from "node:child_process";
import fs from "node:fs";

export async function withLifecycle(work) {
  const controller = new AbortController();
  const children = new Map();
  const cleanups = [];
  const terminate = (signal) => {
    controller.abort(new Error(`interrupted by ${signal}`));
    for (const child of children.keys()) child.kill("SIGTERM");
  };
  const onInt = () => terminate("SIGINT");
  const onTerm = () => terminate("SIGTERM");
  process.once("SIGINT", onInt);
  process.once("SIGTERM", onTerm);
  const scope = {
    signal: controller.signal,
    abort: (error) => {
      controller.abort(error);
      for (const child of children.keys()) child.kill("SIGTERM");
    },
    spawn(command, args, options, graceMs = 5000) {
      controller.signal.throwIfAborted();
      const child = spawn(command, args, options);
      const exited = new Promise((resolve) => {
        child.once("error", (error) => resolve({ error }));
        child.once("exit", (code, signal) => resolve({ code, signal }));
      });
      children.set(child, { exited, graceMs });
      return child;
    },
    async wait(child) {
      const result = await children.get(child).exited;
      controller.signal.throwIfAborted();
      if (result.error) throw result.error;
      if (result.code !== 0) throw new Error(`process failed: ${result.code ?? result.signal}`);
    },
    cleanup(action) {
      cleanups.push(action);
    },
    directory(directory) {
      cleanups.push(() => fs.rmSync(directory, { recursive: true, force: true }));
    },
  };
  let result;
  let failure;
  const errors = [];
  try {
    result = await work(scope);
    controller.signal.throwIfAborted();
  } catch (error) {
    failure = error instanceof Error ? error : new Error(String(error));
  } finally {
    process.off("SIGINT", onInt);
    process.off("SIGTERM", onTerm);
    await Promise.all(
      [...children].map(async ([child, { exited, graceMs }]) => {
        try {
          if (child.exitCode === null && child.signalCode === null && child.pid) {
            child.kill("SIGTERM");
            let timer;
            const done = await Promise.race([
              exited.then(() => true),
              new Promise((resolve) => {
                timer = setTimeout(() => resolve(false), graceMs);
              }),
            ]);
            clearTimeout(timer);
            if (!done) {
              child.kill("SIGKILL");
              await exited;
            }
          }
        } catch (error) {
          errors.push(error);
        }
      }),
    );
    await cleanups.reverse().reduce(
      (previous, cleanup) =>
        previous.then(async () => {
          try {
            await cleanup();
          } catch (error) {
            errors.push(error);
          }
        }),
      Promise.resolve(),
    );
  }
  if (failure && errors.length)
    throw new AggregateError([failure, ...errors], "work and cleanup failed");
  if (failure) throw new AggregateError([failure], failure.message);
  if (errors.length) throw new AggregateError(errors, "cleanup failed");
  return result;
}
