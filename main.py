import asyncio
import fcntl
import ipaddress
import json
import os
import re
import socket
import struct
import time

import decky

MACHINES_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "machines.json")
VIRTUAL_IFACES = ("lo", "docker", "br-", "veth", "virbr", "tailscale", "tun", "wg")
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
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
    # Write then rename, so a crash mid-write can't wipe the saved list
    tmp = MACHINES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(machines, f, indent=2)
    os.replace(tmp, MACHINES_FILE)


def lan_interfaces() -> list[ipaddress.IPv4Interface]:
    """IPv4 address/netmask of each physical interface, read straight from the kernel."""
    ifaces = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            if name.startswith(VIRTUAL_IFACES):
                continue
            req = struct.pack("256s", name.encode()[:15])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, req)[20:24])
            except OSError:
                continue  # interface has no IPv4 address
            iface = ipaddress.IPv4Interface(f"{addr}/{mask}")
            if not iface.ip.is_link_local:
                ifaces.append(iface)
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
    """Ask every IP for its NetBIOS name, collecting answers for `timeout` seconds.

    Sending to each address also makes the kernel ARP for it. ARP is answered
    even when the target's firewall drops everything else (e.g. Windows on a
    "Public" network), so the ARP table is filled in by the time this returns.
    """
    wanted = set(ips)
    names = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        # Datagrams waiting on ARP count against the send buffer; make room for a whole /24
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        for ip in ips:
            try:
                s.sendto(NBSTAT_QUERY, (ip, 137))
            except OSError:
                pass
        s.settimeout(0.2)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if ip in wanted and (name := parse_nbstat(data)):
                names[ip] = name
    return names


def reverse_dns(ip: str) -> str | None:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return None
    return None if name == ip else name.split(".")[0]


class Plugin:
    async def get_machines(self) -> list[dict]:
        return load_machines()

    async def add_machine(self, name: str, mac: str, ip: str | None = None) -> list[dict]:
        mac = normalize_mac(mac)
        machines = [m for m in load_machines() if m["mac"] != mac]
        ip = str(ipaddress.IPv4Address(ip)) if ip else None
        machines.append({"name": name.strip() or "PC", "mac": mac, "ip": ip})
        save_machines(machines)
        return machines

    async def remove_machine(self, mac: str) -> list[dict]:
        machines = [m for m in load_machines() if m["mac"] != mac]
        save_machines(machines)
        return machines

    async def scan(self) -> list[dict]:
        ifaces = lan_interfaces()
        own = {str(i.ip) for i in ifaces}
        targets = []
        for iface in ifaces:
            # Sweep at most the /24 around us: bigger sweeps can overflow the kernel's ARP table
            net = iface.network if iface.network.prefixlen >= 24 else ipaddress.IPv4Interface(f"{iface.ip}/24").network
            targets += [str(h) for h in net.hosts() if str(h) not in own]

        nb = await asyncio.to_thread(netbios_names, targets)
        wanted = set(targets)
        found = {ip: mac for ip, mac in arp_table().items() if ip in wanted}

        machines = load_machines()
        saved = {m["mac"]: m for m in machines}
        new, changed = {}, False
        for ip, mac in found.items():
            if mac not in saved:
                new[ip] = mac
            elif saved[mac]["ip"] != ip:
                # Keep saved machines' IPs current when DHCP hands out a new one
                saved[mac]["ip"], changed = ip, True
        if changed:
            save_machines(machines)

        dns = {ip: asyncio.create_task(asyncio.to_thread(reverse_dns, ip)) for ip in new if ip not in nb}
        if dns:
            await asyncio.wait(dns.values(), timeout=1)
        devices = [
            {"ip": ip, "mac": mac, "name": nb.get(ip) or (dns[ip].result() if dns[ip].done() else None)}
            for ip, mac in new.items()
        ]
        devices.sort(key=lambda d: (d["name"] is None, ipaddress.IPv4Address(d["ip"])))
        decky.logger.info(f"Scan of {len(targets)} addresses on {ifaces} found {len(found)} devices")
        return devices

    async def wake(self, mac: str) -> bool:
        packet = b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16
        targets = {"255.255.255.255"} | {str(i.network.broadcast_address) for i in lan_interfaces()}
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
