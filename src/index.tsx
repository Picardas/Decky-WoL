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

type WakeState = "sending" | "sent" | "failed";

const getMachines = callable<[], Machine[]>("get_machines");
const addMachine = callable<[name: string, mac: string, ip: string | null], Machine[]>("add_machine");
const removeMachine = callable<[mac: string], Machine[]>("remove_machine");
const scan = callable<[], Device[]>("scan");
const wake = callable<[mac: string], boolean>("wake");

// Kept between panel opens (and preloaded at startup) so the wake buttons are
// already rendered when the panel opens, letting the top one take focus at once.
let cachedMachines: Machine[] | null = null;

function errorText(title: string, error: unknown) {
  console.error(title, error);
  return `${title}: ${error instanceof Error ? error.message : String(error)}`;
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
};

function Content() {
  const [machines, setMachinesState] = useState<Machine[] | null>(cachedMachines);
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [editing, setEditing] = useState(false);
  const [wakeState, setWakeState] = useState<Record<string, WakeState>>({});
  const [error, setError] = useState<string | null>(null);
  const timers = useRef<number[]>([]);

  const setMachines = (m: Machine[]) => {
    cachedMachines = m;
    setMachinesState(m);
  };

  useEffect(() => {
    getMachines()
      .then(setMachines)
      .catch((e) => setError(errorText("Couldn't load machines", e)));
    return () => timers.current.forEach(clearTimeout);
  }, []);

  const onWake = async (machine: Machine) => {
    setError(null);
    setWakeState((s) => ({ ...s, [machine.mac]: "sending" }));
    let result: WakeState;
    try {
      result = (await wake(machine.mac)) ? "sent" : "failed";
    } catch (e) {
      setError(errorText(`Couldn't wake ${machine.name}`, e));
      result = "failed";
    }
    setWakeState((s) => ({ ...s, [machine.mac]: result }));
    // Job done: get out of the way. On failure, stay open so the error is visible.
    if (result === "sent") Navigation.CloseSideMenus();
    timers.current.push(
      window.setTimeout(() => {
        setWakeState(({ [machine.mac]: _, ...rest }) => rest);
      }, 5000),
    );
  };

  const onScan = async () => {
    setError(null);
    setScanning(true);
    try {
      setDevices(await scan());
      setMachines(await getMachines());
    } catch (e) {
      setError(errorText("Scan failed", e));
    } finally {
      setScanning(false);
    }
  };

  const onAdd = async (device: Device) => {
    setError(null);
    try {
      setMachines(await addMachine(device.name ?? `PC ${device.ip}`, device.mac, device.ip));
      setDevices((prev) => prev?.filter((d) => d.mac !== device.mac) ?? null);
    } catch (e) {
      setError(errorText("Couldn't add machine", e));
    }
  };

  const onRemove = async (machine: Machine) => {
    setError(null);
    try {
      const remaining = await removeMachine(machine.mac);
      setMachines(remaining);
      if (remaining.length === 0) setEditing(false);
    } catch (e) {
      setError(errorText(`Couldn't remove ${machine.name}`, e));
    }
  };

  return (
    <>
      <PanelSection title="Machines">
        {error && (
          <PanelSectionRow>
            <div style={{ color: "#ff6b6b", wordBreak: "break-word" }}>{error}</div>
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
          // so waking it is a single press of A.
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
        {devices?.length === 0 && (
          <PanelSectionRow>
            <div>No new devices found.</div>
          </PanelSectionRow>
        )}
        {devices?.map((d) => (
          <PanelSectionRow key={d.mac}>
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
    .then((m) => (cachedMachines = m))
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
      cachedMachines = null;
    },
  };
});
