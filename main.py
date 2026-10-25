import asyncio
import ipaddress
import json
import os
import re
import socket
import time

import decky

MACHINES_FILE = os.path.join(decky.DECKY_SETTINGS_DIR, "machines.json")
VIRTUAL_IFACES = ("docker", "br-", "veth", "virbr", "tailscale", "tun", "wg")
# NetBIOS node-status request for "*": Windows PCs answer with their computer name
NBSTAT_QUERY = bytes.fromhex("133700000001000000000000") + b"\x20CK" + b"A" * 30 + bytes.fromhex("0000210001")


def normalize_mac(mac: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def load_machines() -> list[dict]:
    try:
        with open(MACHINES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_machines(machines: list[dict]) -> None:
    os.makedirs(os.path.dirname(MACHINES_FILE), exist_ok=True)
    with open(MACHINES_FILE, "w") as f:
        json.dump(machines, f, indent=2)


async def run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return out.decode()


async def lan_interfaces() -> list[ipaddress.IPv4Interface]:
    out = await run("ip", "-o", "-4", "addr", "show", "scope", "global")
    ifaces = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts and not parts[1].startswith(VIRTUAL_IFACES):
            ifaces.append(ipaddress.IPv4Interface(parts[parts.index("inet") + 1]))
    return ifaces


def arp_table() -> dict[str, str]:
    """IP -> MAC for every neighbour that has answered ARP."""
    table = {}
    with open("/proc/net/arp") as f:
        next(f)
        for line in f:
            ip, _, flags, mac, *_ = line.split()
            if int(flags, 16) & 0x2 and mac != "00:00:00:00:00:00":
                table[ip] = mac.upper()
    return table


def poke(ips: list[str]) -> None:
    """Send an empty UDP datagram to each IP so the kernel ARPs for it.

    ARP is answered even when the target's firewall drops ping, so this finds
    Windows PCs on a "Public" network too.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for ip in ips:
            try:
                s.sendto(b"", (ip, 9))
            except OSError:
                pass


def parse_nbstat(data: bytes) -> str | None:
    try:
        for i in range(data[56]):
            entry = data[57 + i * 18:57 + (i + 1) * 18]
            suffix, flags = entry[15], int.from_bytes(entry[16:18], "big")
            if suffix == 0 and not flags & 0x8000:  # workstation name, not a group
                return entry[:15].decode("ascii", "ignore").strip() or None
    except IndexError:
        pass
    return None


def netbios_names(ips: list[str], timeout: float = 1.5) -> dict[str, str]:
    names = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(0.2)
        for ip in ips:
            try:
                s.sendto(NBSTAT_QUERY, (ip, 137))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(names) < len(ips):
            try:
                data, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if name := parse_nbstat(data):
                names[ip] = name
    return names


def reverse_dns(ip: str) -> str | None:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except OSError:
        return None
    return None if name == ip else name.split(".")[0]


class Plugin:
    async def get_machines(self) -> list[dict]:
        return load_machines()

    async def add_machine(self, name: str, mac: str, ip: str | None = None) -> list[dict]:
        mac = normalize_mac(mac)
        machines = [m for m in load_machines() if m["mac"] != mac]
        machines.append({"name": name.strip() or "PC", "mac": mac, "ip": ip})
        save_machines(machines)
        return machines

    async def remove_machine(self, mac: str) -> list[dict]:
        machines = [m for m in load_machines() if m["mac"] != mac]
        save_machines(machines)
        return machines

    async def scan(self) -> list[dict]:
        ifaces = await lan_interfaces()
        own = {str(i.ip) for i in ifaces}
        targets = []
        for iface in ifaces:
            # Don't sweep huge networks; the /24 around us is where the PC almost always is
            net = iface.network if iface.network.prefixlen >= 22 else ipaddress.IPv4Interface(f"{iface.ip}/24").network
            targets += [str(h) for h in net.hosts() if str(h) not in own]

        for _ in range(2):
            await asyncio.to_thread(poke, targets)
            await asyncio.sleep(1)
        wanted = set(targets)
        found = {ip: mac for ip, mac in arp_table().items() if ip in wanted}

        ips = list(found)
        dns = {ip: asyncio.create_task(asyncio.to_thread(reverse_dns, ip)) for ip in ips}
        nb = await asyncio.to_thread(netbios_names, ips)
        if dns:
            await asyncio.wait(dns.values(), timeout=1)

        machines = load_machines()
        saved = {m["mac"]: m for m in machines}
        devices, changed = [], False
        for ip, mac in found.items():
            if mac in saved:
                # Keep saved machines' IPs current when DHCP hands out a new one
                if saved[mac]["ip"] != ip:
                    saved[mac]["ip"], changed = ip, True
            else:
                name = nb.get(ip) or (dns[ip].result() if dns[ip].done() else None)
                devices.append({"ip": ip, "mac": mac, "name": name})
        if changed:
            save_machines(machines)

        devices.sort(key=lambda d: (d["name"] is None, ipaddress.IPv4Address(d["ip"])))
        decky.logger.info(f"Scan of {len(targets)} addresses found {len(found)} devices")
        return devices

    async def wake(self, mac: str) -> bool:
        packet = b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16
        targets = {"255.255.255.255"} | {str(i.network.broadcast_address) for i in await lan_interfaces()}
        sent = False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(3):
                for addr in targets:
                    for port in (9, 7):
                        try:
                            s.sendto(packet, (addr, port))
                            sent = True
                        except OSError as e:
                            decky.logger.warning(f"Magic packet to {addr}:{port} failed: {e}")
        decky.logger.info(f"Sent magic packet for {mac} to {sorted(targets)}")
        return sent

    async def _main(self):
        decky.logger.info("Wake on LAN loaded")

    async def _unload(self):
        pass
