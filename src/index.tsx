import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaPowerOff } from "react-icons/fa";

interface Machine {
  name: string;
  mac: string;
  ip: string | null;
}

interface Device {
  name: string | null;
  mac: string;
  ip: string;
}

interface ScanResult {
  devices: Device[];
  machines: Machine[];
  error: string | null;
}

type WakeState = "sending" | "sent" | "failed" | "error";

const getMachines = callable<[], Machine[]>("get_machines");
const addMachine = callable<[name: string, mac: string, ip: string | null], Machine[]>("add_machine");
const removeMachine = callable<[mac: string], Machine[]>("remove_machine");
const scan = callable<[], ScanResult>("scan");
const wake = callable<[mac: string], boolean>("wake");

// State kept outside the panel, so it survives the panel closing and reopening:
// - machines are preloaded at startup so the wake buttons are already rendered when
//   the panel opens, letting the top one take focus at once;
// - a scan keeps running (and keeps its results) if the panel is closed meanwhile.
interface Store {
  machines: Machine[] | null;
  devices: Device[] | null;
  scanning: boolean;
  scanError: string | null;
}

const INITIAL_STORE: Store = { machines: null, devices: null, scanning: false, scanError: null };
let store: Store = INITIAL_STORE;
const listeners = new Set<() => void>();
let scanPromise: Promise<void> | null = null;
// Bumped when the plugin unloads, so a scan finishing afterwards doesn't write to the reset store
let generation = 0;

function update(patch: Partial<Store>) {
  store = { ...store, ...patch };
  listeners.forEach((l) => l());
}

function useStore(): Store {
  const [state, setState] = useState(store);
  useEffect(() => {
    const listener = () => setState(store);
    listeners.add(listener);
    listener(); // catch up on anything that changed before we subscribed
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return state;
}

function errorText(title: string, error: unknown) {
  console.error(title, error);
  return `${title}: ${error instanceof Error ? error.message : String(error)}`;
}

function runScan(): Promise<void> {
  if (!scanPromise) {
    const gen = generation;
    const current = () => gen === generation;
    update({ scanning: true, scanError: null });
    scanPromise = scan()
      .then((r) => {
        if (current()) update({ devices: r.devices, machines: r.machines, scanError: r.error });
      })
      .catch((e) => {
        if (current()) update({ scanError: errorText("Scan failed", e) });
      })
      .finally(() => {
        if (current()) {
          scanPromise = null;
          update({ scanning: false });
        }
      });
  }
  return scanPromise;
}

// "10ffe03d" -> "10:FF:E0:3D"
function formatMac(value: string) {
  const hex = value.replace(/[^0-9a-f]/gi, "").slice(0, 12).toUpperCase();
  return hex.match(/.{1,2}/g)?.join(":") ?? "";
}

function AddManuallyModal({
  closeModal,
  onAdded,
  onError,
}: {
  closeModal?: () => void;
  onAdded: (m: Machine[]) => void;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [mac, setMac] = useState("");

  return (
    <ConfirmModal
      strTitle="Add machine"
      strOKButtonText="Add"
      bOKDisabled={mac.length !== 17}
      closeModal={closeModal}
      onOK={async () => {
        try {
          onAdded(await addMachine(name.trim() || "PC", mac, null));
        } catch (e) {
          onError(errorText("Couldn't add machine", e));
        }
      }}
    >
      <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} />
      <TextField
        label="MAC address"
        description="Find it on the PC with: getmac /v"
        value={mac}
        onChange={(e) => setMac(formatMac(e.target.value))}
      />
    </ConfirmModal>
  );
}

const WAKE_STATUS: Record<WakeState, string> = {
  sending: "Sending magic packet…",
  sent: "Magic packet sent",
  failed: "No network connection",
  error: "Couldn't send magic packet",
};

const setMachines = (machines: Machine[]) => update({ machines });

function Content() {
  const { machines, devices, scanning, scanError } = useStore();
  const [editing, setEditing] = useState(false);
  const [wakeState, setWakeState] = useState<Record<string, WakeState>>({});
  const [error, setError] = useState<string | null>(null);
  // One pending "clear the status" timer per machine, so an old one can't clear a newer wake
  const timers = useRef<Record<string, number>>({});

  // A new action replaces whatever went wrong last time, including a stored scan error
  const clearErrors = () => {
    setError(null);
    if (store.scanError) update({ scanError: null });
  };

  useEffect(() => {
    getMachines()
      .then(setMachines)
      .catch((e) => setError(errorText("Couldn't load machines", e)));
    return () => Object.values(timers.current).forEach(clearTimeout);
  }, []);

  const onWake = async (machine: Machine) => {
    clearErrors();
    window.clearTimeout(timers.current[machine.mac]);
    delete timers.current[machine.mac];
    setWakeState((s) => ({ ...s, [machine.mac]: "sending" }));
    let result: WakeState;
    try {
      result = (await wake(machine.mac)) ? "sent" : "failed";
    } catch (e) {
      setError(errorText(`Couldn't wake ${machine.name}`, e));
      result = "error";
    }
    setWakeState((s) => ({ ...s, [machine.mac]: result }));
    // Job done: get out of the way. On failure, stay open so the error is visible.
    if (result === "sent") Navigation.CloseSideMenus();
    timers.current[machine.mac] = window.setTimeout(() => {
      delete timers.current[machine.mac];
      setWakeState(({ [machine.mac]: _, ...rest }) => rest);
    }, 5000);
  };

  const onScan = () => {
    setError(null);
    runScan();
  };

  const onAdd = async (device: Device) => {
    clearErrors();
    try {
      const updated = await addMachine(device.name ?? `PC ${device.ip}`, device.mac, device.ip);
      update({ machines: updated, devices: store.devices?.filter((d) => d.mac !== device.mac) ?? null });
    } catch (e) {
      setError(errorText("Couldn't add machine", e));
    }
  };

  const onRemove = async (machine: Machine) => {
    clearErrors();
    try {
      const remaining = await removeMachine(machine.mac);
      setMachines(remaining);
      if (remaining.length === 0) setEditing(false);
    } catch (e) {
      setError(errorText(`Couldn't remove ${machine.name}`, e));
    }
  };

  const shownError = error ?? scanError;

  return (
    <>
      <PanelSection title="Machines">
        {shownError && (
          <PanelSectionRow>
            <div style={{ color: "var(--gpColor-Red, #ff6b6b)", wordBreak: "break-word" }}>{shownError}</div>
          </PanelSectionRow>
        )}
        {machines?.length === 0 && (
          <PanelSectionRow>
            <div>No machines yet. Turn your PC on and scan below.</div>
          </PanelSectionRow>
        )}
        {machines?.map((m, i) => {
          const state = wakeState[m.mac];
          // The top machine's wake button takes focus as soon as the panel opens,
          // so waking it is a single press of A. `preferredFocus` is the typed prop;
          // `autoFocus` isn't in DialogButtonProps but is passed through to Steam's
          // focus system and is what makes focus land on first open.
          const focusProps = i === 0 && !editing ? { autoFocus: true, preferredFocus: true } : {};
          return (
            <PanelSectionRow key={m.mac}>
              <Field
                childrenLayout="below"
                description={state && !editing ? WAKE_STATUS[state] : m.ip ?? m.mac}
                bottomSeparator="none"
              >
                <DialogButton
                  {...focusProps}
                  disabled={state === "sending"}
                  onClick={() => (editing ? onRemove(m) : onWake(m))}
                >
                  <span style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                    {editing ? `Remove ${m.name}` : <><FaPowerOff /> {m.name}</>}
                  </span>
                </DialogButton>
              </Field>
            </PanelSectionRow>
          );
        })}
        {!!machines?.length && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setEditing(!editing)}>
              {editing ? "Done" : "Edit list"}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Add machine">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={scanning}
            description="The PC must be switched on to be found."
            onClick={onScan}
          >
            {scanning ? "Scanning…" : "Scan network"}
          </ButtonItem>
        </PanelSectionRow>
        {devices?.length === 0 && !scanError && (
          <PanelSectionRow>
            <div>No new devices found.</div>
          </PanelSectionRow>
        )}
        {devices?.map((d) => (
          <PanelSectionRow key={`${d.ip}-${d.mac}`}>
            <ButtonItem layout="below" description={`${d.ip}  ·  ${d.mac}`} onClick={() => onAdd(d)}>
              + {d.name ?? "Unknown device"}
            </ButtonItem>
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => showModal(<AddManuallyModal onAdded={setMachines} onError={setError} />)}
          >
            Add by MAC address
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Load the saved machines in the background at startup, before the panel is first opened
  getMachines()
    .then(setMachines)
    .catch((e) => console.error("Wake on LAN: couldn't preload machines", e));

  return {
    // The name shown in various decky menus
    name: "Wake on LAN",
    // The element displayed at the top of your plugin's menu
    titleView: <div className={staticClasses.Title}>Wake on LAN</div>,
    // The content of your plugin's menu
    content: <Content />,
    // The icon displayed in the plugin list
    icon: <FaPowerOff />,
    // The function triggered when your plugin unloads
    onDismount() {
      generation++;
      scanPromise = null;
      store = INITIAL_STORE;
      listeners.clear();
    },
  };
});
