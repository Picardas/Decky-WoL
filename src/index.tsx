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

const MAC_PATTERN = /^([0-9a-f]{2}[:-]?){5}[0-9a-f]{2}$/i;

function AddManuallyModal({ closeModal, onAdded }: { closeModal?: () => void; onAdded: (m: Machine[]) => void }) {
  const [name, setName] = useState("");
  const [mac, setMac] = useState("");

  return (
    <ConfirmModal
      strTitle="Add machine"
      strOKButtonText="Add"
      bOKDisabled={!MAC_PATTERN.test(mac.trim())}
      closeModal={closeModal}
      onOK={async () => onAdded(await addMachine(name.trim() || "PC", mac.trim(), null))}
    >
      <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} />
      <TextField
        label="MAC address"
        description="Find it on the PC with: getmac /v"
        value={mac}
        onChange={(e) => setMac(e.target.value)}
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
    getMachines().then(setMachines);
  }, []);

  const onWake = async (machine: Machine) => {
    const sent = await wake(machine.mac);
    toaster.toast({
      title: sent ? `Waking ${machine.name}` : `Couldn't wake ${machine.name}`,
      body: sent ? "Magic packet sent" : "No network connection",
    });
  };

  const onScan = async () => {
    setScanning(true);
    try {
      setDevices(await scan());
      setMachines(await getMachines());
    } finally {
      setScanning(false);
    }
  };

  const onAdd = async (device: Device) => {
    setMachines(await addMachine(device.name ?? `PC ${device.ip}`, device.mac, device.ip));
    setDevices((prev) => prev?.filter((d) => d.mac !== device.mac) ?? null);
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
              onClick={async () => (editing ? setMachines(await removeMachine(m.mac)) : onWake(m))}
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
