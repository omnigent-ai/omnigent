import { useState } from "react";
import { createRoot } from "react-dom/client";
import * as Dialog from "radix-ui/dialog";

window["__reproEvents"] = [];
for (const type of ["pointerdown", "focusin", "focusout", "keydown", "input", "submit"]) {
  document.addEventListener(
    type,
    (event) => {
      window["__reproEvents"].push({
        type,
        target: event.target.id,
        key: event.key,
        value: event.target.value,
        active: document.activeElement?.id,
        trusted: event.isTrusted,
      });
    },
    true,
  );
}

function CapacityDialog() {
  const [values, setValues] = useState({
    name: "test",
    resource: "Reno - Pack-out 2",
    period: "W41",
    "available-units": "3540",
  });
  return (
    <main>
      <h1>Capacity planning</h1>
      <p>Real Radix modal with a transformed, scrollable form and controlled inputs.</p>
      <Dialog.Root defaultOpen>
        <Dialog.Trigger asChild>
          <button id="create-scenario" type="button">
            Create capacity scenario
          </button>
        </Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content asChild>
            <form
              id="capacity-dialog"
              className="dialog-content"
              onSubmit={(event) => event.preventDefault()}
            >
              <Dialog.Title>Capacity assumptions</Dialog.Title>
              <Dialog.Description>Adjust the scenario inputs below.</Dialog.Description>
              {[
                ["name", "Name"],
                ["resource", "Resource"],
                ["period", "Period"],
                ["available-units", "Available units"],
              ].map(([key, label]) => (
                <label key={key} htmlFor={`scenario-${key}`}>
                  {label}
                  <input
                    id={`scenario-${key}`}
                    name={key}
                    value={values[key]}
                    autoComplete="off"
                    onChange={(event) =>
                      setValues((previous) => ({ ...previous, [key]: event.target.value }))
                    }
                  />
                </label>
              ))}
              <output id="scenario-values">Period: {values.period}</output>
              <div className="scroll-space" aria-hidden="true" />
              <div className="actions">
                <Dialog.Close asChild>
                  <button id="cancel-scenario" type="button">
                    Cancel
                  </button>
                </Dialog.Close>
                <button id="save-scenario" type="submit">
                  Save scenario
                </button>
              </div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<CapacityDialog />);
