import {
  ButtonItem,
  ConfirmModal,
  PanelSection,
  PanelSectionRow,
  TextField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useState } from "react";
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

const getMachines = callable<[], Machine[]>("get_machines");
const addMachine = callable<[name: string, mac: string, ip: string | null], Machine[]>("add_machine");
const removeMachine = callable<[mac: string], Machine[]>("remove_machine");
const scan = callable<[], Device[]>("scan");
const wake = callable<[mac: string], boolean>("wake");

function showError(title: string, error: unknown) {
  console.error(title, error);
  toaster.toast({ title, body: error instanceof Error ? error.message : String(error) });
}

// "10ffe03d" -> "10:FF:E0:3D"
function formatMac(value: string) {
  const hex = value.replace(/[^0-9a-f]/gi, "").slice(0, 12).toUpperCase();
  return hex.match(/.{1,2}/g)?.join(":") ?? "";
}

function AddManuallyModal({ closeModal, onAdded }: { closeModal?: () => void; onAdded: (m: Machine[]) => void }) {
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
          showError("Couldn't add machine", e);
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

function Content() {
  const [machines, setMachines] = useState<Machine[]>([]);
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    getMachines()
      .then(setMachines)
      .catch((e) => showError("Couldn't load machines", e));
  }, []);

  const onWake = async (machine: Machine) => {
    try {
      const sent = await wake(machine.mac);
      toaster.toast({
        title: sent ? `Waking ${machine.name}` : `Couldn't wake ${machine.name}`,
        body: sent ? "Magic packet sent" : "No network connection",
      });
    } catch (e) {
      showError(`Couldn't wake ${machine.name}`, e);
    }
  };

  const onScan = async () => {
    setScanning(true);
    try {
      setDevices(await scan());
      setMachines(await getMachines());
    } catch (e) {
      showError("Scan failed", e);
    } finally {
      setScanning(false);
    }
  };

  const onAdd = async (device: Device) => {
    try {
      setMachines(await addMachine(device.name ?? `PC ${device.ip}`, device.mac, device.ip));
      setDevices((prev) => prev?.filter((d) => d.mac !== device.mac) ?? null);
    } catch (e) {
      showError("Couldn't add machine", e);
    }
  };

  const onRemove = async (machine: Machine) => {
    try {
      const remaining = await removeMachine(machine.mac);
      setMachines(remaining);
      if (remaining.length === 0) setEditing(false);
    } catch (e) {
      showError(`Couldn't remove ${machine.name}`, e);
    }
  };

  return (
    <>
      <PanelSection title="Machines">
        {machines.length === 0 && (
          <PanelSectionRow>
            <div>No machines yet. Turn your PC on and scan below.</div>
          </PanelSectionRow>
        )}
        {machines.map((m) => (
          <PanelSectionRow key={m.mac}>
            <ButtonItem
              layout="below"
              description={m.ip ?? m.mac}
              onClick={() => (editing ? onRemove(m) : onWake(m))}
            >
              <span style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                {editing ? `Remove ${m.name}` : <><FaPowerOff /> {m.name}</>}
              </span>
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {machines.length > 0 && (
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
          <ButtonItem layout="below" onClick={() => showModal(<AddManuallyModal onAdded={setMachines} />)}>
            Add by MAC address
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "Wake on LAN",
  titleView: <div className={staticClasses.Title}>Wake on LAN</div>,
  content: <Content />,
  icon: <FaPowerOff />,
}));
