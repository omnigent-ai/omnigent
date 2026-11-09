import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as Select from "radix-ui/select";

const LOGGED_EVENTS = [
  "pointerdown",
  "mousedown",
  "mouseup",
  "click",
  "focusin",
  "input",
  "change",
];
const REGIONS = ["us-east-1", "us-west-2", "eu-west-1"];
const ENVIRONMENTS = ["canary", "production"];

window["__dropdownEvents"] = [];
const listeners = new Set();
for (const type of LOGGED_EVENTS) {
  document.addEventListener(
    type,
    (event) => {
      const field = event.target.closest?.("[data-field]")?.getAttribute("data-field");
      if (!field) return;
      window["__dropdownEvents"].push({ type, field, trusted: event.isTrusted });
      for (const listener of listeners) listener();
    },
    true,
  );
}

function useEventLog() {
  const [events, setEvents] = useState([]);
  useEffect(() => {
    const update = () => setEvents([...window["__dropdownEvents"]]);
    listeners.add(update);
    return () => listeners.delete(update);
  }, []);
  return events;
}

// Opens on mousedown and filters on typing, like react-select's control and input.
function MouseDownSelect({ value, onChange, open, setOpen }) {
  const [query, setQuery] = useState("");
  const inputRef = useRef(null);
  const options = REGIONS.filter((region) => region.includes(query));
  return (
    <div className="custom-select" data-field="Region">
      <div
        className="custom-control"
        data-open={open}
        onMouseDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          inputRef.current.focus();
          setOpen((previous) => !previous);
        }}
      >
        {!query && <span className="custom-value">{value ?? "Select..."}</span>}
        <input
          ref={inputRef}
          id="region-input"
          role="combobox"
          aria-label="Region"
          aria-expanded={open}
          autoComplete="off"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onBlur={() => setOpen(false)}
        />
        <span aria-hidden="true">▾</span>
      </div>
      {open && (
        <div role="listbox" className="custom-menu">
          {options.map((region) => (
            <div
              key={region}
              role="option"
              aria-selected={region === value}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onChange(region);
                setQuery("");
                setOpen(false);
              }}
            >
              {region}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DropdownForm() {
  const [region, setRegion] = useState(null);
  const [account, setAccount] = useState("dev");
  const [environment, setEnvironment] = useState("");
  const [regionOpen, setRegionOpen] = useState(false);
  const events = useEventLog();
  return (
    <main>
      <h1>Deploy stage configuration</h1>
      <label htmlFor="region-input">Region (custom dropdown, opens on mousedown)</label>
      <MouseDownSelect
        value={region}
        onChange={setRegion}
        open={regionOpen}
        setOpen={setRegionOpen}
      />
      <label htmlFor="account">Account (native select)</label>
      <select
        id="account"
        data-field="Account"
        value={account}
        onChange={(event) => setAccount(event.target.value)}
      >
        <option value="dev">dev</option>
        <option value="staging">staging</option>
        <option value="prod">prod</option>
      </select>
      <span className="field-label">Environment (Radix Select)</span>
      <Select.Root value={environment} onValueChange={setEnvironment}>
        <Select.Trigger className="radix-trigger" aria-label="Environment" data-field="Environment">
          <Select.Value placeholder="Pick environment" />
        </Select.Trigger>
        <Select.Portal>
          <Select.Content className="radix-content" position="popper" data-field="Environment">
            <Select.Viewport>
              {ENVIRONMENTS.map((name) => (
                <Select.Item key={name} value={name} className="radix-item">
                  <Select.ItemText>{name}</Select.ItemText>
                </Select.Item>
              ))}
            </Select.Viewport>
          </Select.Content>
        </Select.Portal>
      </Select.Root>
      <output id="dropdown-state">
        Region: {region ?? "none"} ({regionOpen ? "menu open" : "menu closed"}) · Account: {account}{" "}
        · Environment: {environment || "none"}
      </output>
      <div id="event-log" aria-hidden="true">
        <strong>Page events</strong>
        {events.slice(-6).map((event, index) => (
          // oxlint-disable-next-line react/no-array-index-key -- The log is append-only.
          <div key={index}>
            {event.type} on {event.field} · {event.trusted ? "trusted" : "untrusted"}
          </div>
        ))}
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<DropdownForm />);
