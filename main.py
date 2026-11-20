import asyncio
import contextlib
import fcntl
import ipaddress
import json
import os
import re
import socket
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import decky

MACHINES_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "machines.json")
# Name prefixes of loopback, container, VPN and overlay interfaces: never scanned or woken through
VIRTUAL_IFACES = (
    "lo", "docker", "br-", "veth", "virbr", "tailscale", "tun", "tap", "wg", "zt",
    "ppp", "wwan", "nordlynx", "proton",
)
SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
IFF_UP, IFF_BROADCAST, IFF_LOOPBACK, IFF_POINTOPOINT, IFF_NOARP = 0x1, 0x2, 0x8, 0x10, 0x80
ARPHRD_ETHER = 1  # /sys/class/net/<if>/type for both Ethernet and Wi-Fi
# NetBIOS node-status request for "*": Windows PCs answer with their computer name
NBSTAT_TXID = b"\x13\x37"
NBSTAT_QUERY = NBSTAT_TXID + bytes.fromhex("00000001000000000000") + b"\x20CK" + b"A" * 30 + bytes.fromhex("0000210001")
NETBIOS_WINDOW = 1.5  # seconds to collect NetBIOS answers (and let ARP complete)
DNS_WAIT = 1.0  # seconds to wait for reverse DNS before showing devices unnamed
NAME_MAX = 64

NO_NETWORK = "No network connection"
NOT_PRIVATE = "Scanning only works on a private home network. Add the PC by MAC address instead."

# Reverse DNS can hang for seconds and can't be cancelled, so it gets its own small pool:
# slow lookups never starve the default executor, and a rescan reuses lookups still running.
_dns_pool: ThreadPoolExecutor | None = None
_dns_pending: dict[str, asyncio.Future] = {}
# Strong references to fire-and-forget tasks, so they aren't garbage-collected mid-run
_background: set[asyncio.Task] = set()


def clean_name(name: str) -> str:
    """Strip control/format characters (incl. bidi overrides) and cap the length."""
    return "".join(c for c in name if c.isprintable()).strip()[:NAME_MAX].strip()


def normalize_mac(mac: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def _valid_machine(entry) -> dict | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("mac"), str):
        return None
    try:
        mac = normalize_mac(entry["mac"])
    except ValueError:
        return None
    ip = entry.get("ip")
    try:
        ip = str(ipaddress.IPv4Address(ip)) if isinstance(ip, str) and ip else None
    except ValueError:
        ip = None  # refreshed by the next scan
    return {"name": clean_name(entry["name"]) or "PC", "mac": mac, "ip": ip}


def _quarantine(reason: str) -> None:
    """Move a damaged machines file aside so the next save can't destroy what's in it."""
    backup = f"{MACHINES_FILE}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    os.replace(MACHINES_FILE, backup)
    decky.logger.warning(f"Saved machines file was {reason}; moved it to {backup}")


def load_machines() -> list[dict]:
    try:
        with open(MACHINES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
        _quarantine(f"unreadable ({e})")
        return []
    if not isinstance(data, list):
        _quarantine("not a list")
        return []
    machines, seen = [], set()
    for entry in data:
        machine = _valid_machine(entry)
        if machine and machine["mac"] not in seen:
            seen.add(machine["mac"])
            machines.append(machine)
    if len(machines) != len(data):
        _quarantine(f"partly invalid ({len(data) - len(machines)} entries dropped)")
        save_machines(machines)
    return machines


def save_machines(machines: list[dict]) -> None:
    directory = os.path.dirname(MACHINES_FILE)
    os.makedirs(directory, exist_ok=True)
    # Write a private temp file, flush it to disk, then rename over the old one,
    # so a crash or power loss mid-write can't wipe the saved list
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".machines-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(machines, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MACHINES_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _is_ethernet(name: str) -> bool:
    try:
        with open(f"/sys/class/net/{name}/type") as f:
            return int(f.read()) == ARPHRD_ETHER
    except (OSError, ValueError):
        return False


def lan_interfaces() -> list[ipaddress.IPv4Interface]:
    """IPv4 address/netmask of each physical, broadcast-capable interface that is up."""
    ifaces = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            if name.startswith(VIRTUAL_IFACES) or not _is_ethernet(name):
                continue
            req = struct.pack("256s", name.encode()[:15])
            try:
                flags = struct.unpack_from("H", fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, req), 16)[0]
                if flags & (IFF_LOOPBACK | IFF_POINTOPOINT | IFF_NOARP) or ~flags & (IFF_UP | IFF_BROADCAST):
                    continue
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, req)[20:24])
            except OSError:
                continue  # interface has no IPv4 address
            iface = ipaddress.IPv4Interface(f"{addr}/{mask}")
            if not iface.ip.is_link_local:
                ifaces.append(iface)
    return ifaces


def scan_networks(ifaces: list[ipaddress.IPv4Interface]) -> list[ipaddress.IPv4Network]:
    """The non-public networks to sweep, each at most the /24 around us, without duplicates."""
    nets = {}
    for iface in ifaces:
        if iface.ip.is_global or iface.ip.is_loopback:
            continue  # never probe strangers' hosts on public-IP networks
        # Bigger sweeps can overflow the kernel's ARP table
        net = iface.network if iface.network.prefixlen >= 24 else ipaddress.IPv4Interface(f"{iface.ip}/24").network
        nets[net] = None
    return list(nets)


def arp_table(path: str = "/proc/net/arp") -> dict[str, str]:
    """IP -> MAC for every neighbour that has answered ARP."""
    table = {}
    with open(path) as f:
        next(f, None)
        for line in f:
            fields = line.split()
            if len(fields) < 4:
                continue
            ip, _, flags, mac = fields[:4]
            try:
                complete = int(flags, 16) & 0x2
            except ValueError:
                continue
            if complete and mac != "00:00:00:00:00:00":
                table[ip] = mac.upper()
    return table


def pick_ips(arp: dict[str, str], named: dict[str, str]) -> dict[str, str]:
    """MAC -> one IP. Several IPs can share a MAC (Wi-Fi extenders, multi-homed hosts):
    prefer the one that told us its name, then the lowest address."""
    best = {}
    for ip, mac in sorted(arp.items(), key=lambda kv: (kv[0] not in named, ipaddress.IPv4Address(kv[0]))):
        best.setdefault(mac, ip)
    return best


def parse_nbstat(data: bytes) -> str | None:
    # Header (12) + echoed name (34) + type, class, TTL, length (10) + name count (1)
    if (
        len(data) < 57
        or data[:2] != NBSTAT_TXID
        or not data[2] & 0x80  # a response
        or data[3] & 0x0F  # RCODE: error
        or data[46:48] != b"\x00\x21"  # NBSTAT record
    ):
        return None
    for i in range(data[56]):
        entry = data[57 + i * 18:57 + (i + 1) * 18]
        if len(entry) < 18:
            break
        suffix, flags = entry[15], int.from_bytes(entry[16:18], "big")
        if suffix == 0 and not flags & 0x8000:  # workstation name, not a group
            return clean_name(entry[:15].decode("ascii", "ignore")) or None
    return None


def netbios_names(ips: list[str], timeout: float = NETBIOS_WINDOW) -> dict[str, str]:
    """Ask every IP for its NetBIOS name, collecting answers for `timeout` seconds.

    Sending to each address also makes the kernel ARP for it. ARP is answered
    even when the target's firewall drops everything else (e.g. Windows on a
    "Public" network), so the ARP table is filled in by the time this returns.
    """
    wanted = set(ips)
    names = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        # Datagrams waiting on ARP count against the send buffer, so ask for plenty.
        # Unprivileged, the kernel caps this at net.core.wmem_max, which still fits a /24.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        for ip in ips:
            try:
                s.sendto(NBSTAT_QUERY, (ip, 137))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            s.settimeout(remaining)
            try:
                data, (ip, port) = s.recvfrom(2048)
            except OSError:  # includes the timeout
                break
            if port == 137 and ip in wanted and (name := parse_nbstat(data)):
                names[ip] = name
    return names


def reverse_dns(ip: str) -> str | None:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return None
    return None if name == ip else clean_name(name.split(".")[0]) or None


def _lookup_name(ip: str) -> asyncio.Future:
    global _dns_pool
    if (fut := _dns_pending.get(ip)) is None:
        if _dns_pool is None:
            _dns_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="wol-dns")
        fut = asyncio.get_running_loop().run_in_executor(_dns_pool, reverse_dns, ip)
        _dns_pending[ip] = fut

        def done(f: asyncio.Future) -> None:
            _dns_pending.pop(ip, None)
            if not f.cancelled():
                f.exception()  # mark retrieved: nobody may be waiting any more

        fut.add_done_callback(done)
    return fut


def _lookup_result(fut: asyncio.Future | None) -> str | None:
    if fut is None or not fut.done() or fut.cancelled() or fut.exception():
        return None
    return fut.result()


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16


def send_magic(packet: bytes, targets: list[str]) -> bool:
    sent = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in targets:
            for port in (9, 7):
                try:
                    s.sendto(packet, (addr, port))
                    sent = True
                except OSError as e:
                    decky.logger.warning(f"Magic packet to {addr}:{port} failed: {e}")
    return sent


async def _repeat_wake(packet: bytes, targets: list[str]) -> None:
    # Repeat a little apart, so one burst of Wi-Fi loss can't swallow every copy
    try:
        for _ in range(2):
            await asyncio.sleep(0.1)
            send_magic(packet, targets)
    except Exception as e:
        decky.logger.warning(f"Repeating magic packet failed: {e}")


def _require_str(*values) -> None:
    if not all(isinstance(v, str) for v in values):
        raise ValueError("Invalid machine details")


class Plugin:
    async def get_machines(self) -> list[dict]:
        return load_machines()

    async def add_machine(self, name: str, mac: str, ip: str | None = None) -> list[dict]:
        _require_str(name, mac)
        if ip is not None:
            _require_str(ip)
        mac = normalize_mac(mac)
        ip = str(ipaddress.IPv4Address(ip)) if ip else None
        name = clean_name(name) or "PC"
        machines = load_machines()
        for m in machines:
            if m["mac"] == mac:
                # Already saved: rename it in place, keeping its spot in the list
                m["name"] = name
                m["ip"] = ip or m["ip"]
                break
        else:
            machines.append({"name": name, "mac": mac, "ip": ip})
        save_machines(machines)
        return machines

    async def remove_machine(self, mac: str) -> list[dict]:
        _require_str(mac)
        mac = normalize_mac(mac)
        machines = [m for m in load_machines() if m["mac"] != mac]
        save_machines(machines)
        return machines

    async def scan(self) -> dict:
        ifaces = lan_interfaces()
        nets = scan_networks(ifaces)
        if not nets:
            return {"devices": [], "machines": load_machines(), "error": NOT_PRIVATE if ifaces else NO_NETWORK}

        own = {str(i.ip) for i in ifaces}
        targets = list(dict.fromkeys(str(h) for net in nets for h in net.hosts() if str(h) not in own))
        nb = await asyncio.to_thread(netbios_names, targets)
        wanted = set(targets)
        found = pick_ips({ip: mac for ip, mac in arp_table().items() if ip in wanted}, nb)

        saved = {m["mac"] for m in load_machines()}
        new = {mac: ip for mac, ip in found.items() if mac not in saved}
        lookups = {ip: _lookup_name(ip) for ip in new.values() if ip not in nb}
        if lookups:
            await asyncio.wait(lookups.values(), timeout=DNS_WAIT)

        # Re-read after the waits, so a machine added in the meantime isn't overwritten
        machines = load_machines()
        changed = False
        for m in machines:
            # Keep saved machines' IPs current when DHCP hands out a new one
            if (ip := found.get(m["mac"])) and m["ip"] != ip:
                m["ip"], changed = ip, True
        if changed:
            save_machines(machines)
        saved = {m["mac"] for m in machines}

        devices = [
            {"ip": ip, "mac": mac, "name": nb.get(ip) or _lookup_result(lookups.get(ip))}
            for mac, ip in new.items()
            if mac not in saved
        ]
        devices.sort(key=lambda d: (d["name"] is None, ipaddress.IPv4Address(d["ip"])))
        decky.logger.info(f"Scanned {len(targets)} addresses: {len(found)} devices, {len(devices)} new")
        decky.logger.debug(f"Scanned networks {nets} from {ifaces}")
        return {"devices": devices, "machines": machines, "error": None}

    async def wake(self, mac: str) -> bool:
        _require_str(mac)
        packet = magic_packet(mac)
        targets = sorted({"255.255.255.255"} | {str(i.network.broadcast_address) for i in lan_interfaces()})
        sent = send_magic(packet, targets)
        if sent:
            # Return straight away so the menu can close; the repeats follow in the background
            task = asyncio.create_task(_repeat_wake(packet, targets))
            _background.add(task)
            task.add_done_callback(_background.discard)
        decky.logger.info(f"Magic packet {'sent' if sent else 'not sent'} on {len(targets)} broadcast address(es)")
        decky.logger.debug(f"Magic packet for {mac} to {targets}")
        return sent

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        decky.logger.info("Wake on LAN loaded")

    # Called first during unload, when the plugin is stopped but not removed
    async def _unload(self):
        global _dns_pool
        for task in list(_background):
            task.cancel()
        if _dns_pool is not None:
            # Lookups already blocked in the resolver can't be interrupted; they end with the process
            _dns_pool.shutdown(wait=False, cancel_futures=True)
            _dns_pool = None
        _dns_pending.clear()
        decky.logger.info("Wake on LAN unloaded")

    # Called after `_unload` during uninstall. Saved machines are kept in the
    # settings dir so a reinstall picks them up again.
    async def _uninstall(self):
        decky.logger.info("Wake on LAN uninstalled")
