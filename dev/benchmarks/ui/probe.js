// Two animation frames include React's commit and a rendering opportunity.
// This is a key-to-frame proxy, not the Event Timing API's INP metric.
(() => {
  let sample;
  window.omnigentTypingBenchmark = {
    arm(textarea) {
      sample = new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          textarea.removeEventListener("keydown", onKeyDown, true);
          reject(
            new Error("No composer key-to-frame sample within 10 seconds"),
          );
        }, 10_000);
        function onKeyDown(event) {
          const start = event.timeStamp;
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              clearTimeout(timeout);
              resolve({
                milliseconds: performance.now() - start,
                value: textarea.value,
              });
            });
          });
        }
        textarea.addEventListener("keydown", onKeyDown, {
          capture: true,
          once: true,
        });
      });
      // The driver reads the promise after sending a real keyboard event.
      sample.catch(() => {});
    },
    read() {
      return sample;
    },
  };
})();
