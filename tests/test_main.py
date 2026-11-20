"""Backend tests. Run with: python3 -m unittest discover -s tests"""

import asyncio
import json
import logging
import os
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

# main.py imports the `decky` module that Decky Loader provides at runtime; stub it
_settings = tempfile.mkdtemp(prefix="wol-test-")
_decky = types.ModuleType("decky")
_decky.DECKY_PLUGIN_SETTINGS_DIR = _settings
_decky.logger = logging.getLogger("decky-wol-test")
_decky.logger.addHandler(logging.NullHandler())
_decky.logger.propagate = False
sys.modules["decky"] = _decky
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

MAC = "10:FF:E0:3D:AA:01"


def nbstat_response(name: str = "GAMING-PC", txid: bytes = b"\x13\x37", flags: bytes = b"\x84\x00") -> bytes:
    header = txid + flags + bytes.fromhex("0000000100000000")
    question = b"\x20CK" + b"A" * 30 + b"\x00"
    rr = bytes.fromhex("0021") + bytes.fromhex("0001") + bytes(4) + bytes.fromhex("0041")
    entries = [
        name.encode().ljust(15) + b"\x00" + b"\x04\x00",  # workstation name
        b"WORKGROUP".ljust(15) + b"\x00" + b"\x84\x00",  # group name
    ]
    return header + question + rr + bytes([len(entries)]) + b"".join(entries) + bytes(6)


class TestHelpers(unittest.TestCase):
    def test_normalize_mac(self):
        self.assertEqual(main.normalize_mac("10-ff-e0-3d-aa-01"), MAC)
        self.assertEqual(main.normalize_mac("10ffe03daa01"), MAC)
        with self.assertRaises(ValueError):
            main.normalize_mac("10:ff:e0")

    def test_clean_name(self):
        self.assertEqual(main.clean_name("  PC\x00\x1b[31m‮evil "), "PC[31mevil")
        self.assertEqual(len(main.clean_name("x" * 500)), main.NAME_MAX)

    def test_parse_nbstat(self):
        self.assertEqual(main.parse_nbstat(nbstat_response()), "GAMING-PC")

    def test_parse_nbstat_rejects_bad_packets(self):
        good = nbstat_response()
        self.assertIsNone(main.parse_nbstat(nbstat_response(txid=b"\xde\xad")))
        self.assertIsNone(main.parse_nbstat(nbstat_response(flags=b"\x04\x00")))  # a query, not a response
        self.assertIsNone(main.parse_nbstat(nbstat_response(flags=b"\x84\x03")))  # RCODE error
        self.assertIsNone(main.parse_nbstat(good[:40]))
        # Name count says 2 but the first entry is cut short
        self.assertIsNone(main.parse_nbstat(good[:57 + 16]))

    def test_parse_nbstat_strips_control_characters(self):
        self.assertEqual(main.parse_nbstat(nbstat_response("PC\x07\x1b")), "PC")

    def test_arp_table(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(
                "IP address       HW type     Flags       HW address            Mask     Device\n"
                "192.168.1.10     0x1         0x2         10:ff:e0:3d:aa:01     *        wlan0\n"
                "192.168.1.11     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
                "garbage\n"
                "192.168.1.12     0x1         zz          10:ff:e0:3d:aa:02     *        wlan0\n"
            )
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(main.arp_table(f.name), {"192.168.1.10": MAC})

    def test_pick_ips_dedupes_shared_mac(self):
        arp = {"192.168.1.20": MAC, "192.168.1.5": MAC, "192.168.1.9": "AA:AA:AA:AA:AA:AA"}
        self.assertEqual(main.pick_ips(arp, {}), {MAC: "192.168.1.5", "AA:AA:AA:AA:AA:AA": "192.168.1.9"})
        self.assertEqual(main.pick_ips(arp, {"192.168.1.20": "PC"})[MAC], "192.168.1.20")

    def test_scan_networks(self):
        from ipaddress import IPv4Interface as I
        nets = main.scan_networks([I("192.168.1.5/24"), I("192.168.1.6/24"), I("10.0.0.5/8"), I("100.64.3.4/10"), I("8.8.8.8/24")])
        self.assertEqual([str(n) for n in nets], ["192.168.1.0/24", "10.0.0.0/24", "100.64.3.0/24"])


class MachinesFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wol-machines-")
        patcher = mock.patch.object(main, "MACHINES_FILE", os.path.join(self.dir, "machines.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin = main.Plugin()

    def write(self, content: str):
        with open(main.MACHINES_FILE, "w") as f:
            f.write(content)

    def backups(self):
        return [n for n in os.listdir(self.dir) if ".corrupt-" in n]

    def run_async(self, coro):
        return asyncio.run(coro)


class TestLoadSave(MachinesFileTest):
    def test_missing_file(self):
        self.assertEqual(main.load_machines(), [])

    def test_roundtrip_and_permissions(self):
        machines = [{"name": "PC", "mac": MAC, "ip": "192.168.1.10"}]
        main.save_machines(machines)
        self.assertEqual(main.load_machines(), machines)
        self.assertEqual(stat.S_IMODE(os.stat(main.MACHINES_FILE).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_truncated_file_is_backed_up_not_lost(self):
        self.write('[{"name": "PC", "mac": "' + MAC)
        self.assertEqual(main.load_machines(), [])
        self.assertEqual(len(self.backups()), 1)
        self.assertFalse(os.path.exists(main.MACHINES_FILE))

    def test_non_utf8_file_is_backed_up(self):
        with open(main.MACHINES_FILE, "wb") as f:
            f.write(b"\xff\xfe\x00garbage")
        self.assertEqual(main.load_machines(), [])
        self.assertEqual(len(self.backups()), 1)

    def test_wrong_shapes(self):
        for content in ("null", '{"a": 1}', "1"):
            with self.subTest(content=content):
                self.write(content)
                self.assertEqual(main.load_machines(), [])

    def test_partly_invalid_list_keeps_good_entries_and_backs_up(self):
        self.write(json.dumps([
            {"name": "Good", "mac": "10ffe03daa01", "ip": "not-an-ip"},
            {"name": "x"},
            1,
            {"name": "Dup", "mac": MAC, "ip": None},
        ]))
        self.assertEqual(main.load_machines(), [{"name": "Good", "mac": MAC, "ip": None}])
        self.assertEqual(len(self.backups()), 1)
        # The cleaned list was saved, so the next load is clean and makes no new backup
        self.assertEqual(main.load_machines(), [{"name": "Good", "mac": MAC, "ip": None}])
        self.assertEqual(len(self.backups()), 1)


class TestCallables(MachinesFileTest):
    def test_add_remove(self):
        added = self.run_async(self.plugin.add_machine("  PC\x00 ", "10-ff-e0-3d-aa-01", "192.168.1.10"))
        self.assertEqual(added, [{"name": "PC", "mac": MAC, "ip": "192.168.1.10"}])
        self.assertEqual(self.run_async(self.plugin.remove_machine("10ffe03daa01")), [])

    def test_add_existing_updates_in_place(self):
        self.run_async(self.plugin.add_machine("A", MAC, "192.168.1.10"))
        self.run_async(self.plugin.add_machine("B", "AA:AA:AA:AA:AA:AA", None))
        machines = self.run_async(self.plugin.add_machine("Renamed", MAC, None))
        self.assertEqual(machines[0], {"name": "Renamed", "mac": MAC, "ip": "192.168.1.10"})
        self.assertEqual(len(machines), 2)

    def test_rejects_bad_types(self):
        for args in ((123, MAC, None), ("PC", 5, None), ("PC", MAC, True), ("PC", MAC, "999.1.1.1")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.run_async(self.plugin.add_machine(*args))
        with self.assertRaises(ValueError):
            self.run_async(self.plugin.remove_machine(None))
        with self.assertRaises(ValueError):
            self.run_async(self.plugin.wake(None))


class TestScan(MachinesFileTest):
    def scan(self, arp, names=None, ifaces=("192.168.1.2/24",), on_dns=None):
        from ipaddress import IPv4Interface

        def fake_dns(ip):
            if on_dns:
                on_dns()
            return None

        with mock.patch.object(main, "lan_interfaces", return_value=[IPv4Interface(i) for i in ifaces]), \
                mock.patch.object(main, "netbios_names", return_value=names or {}), \
                mock.patch.object(main, "arp_table", return_value=arp), \
                mock.patch.object(main, "reverse_dns", side_effect=fake_dns):
            return self.run_async(self.plugin.scan())

    def test_no_network(self):
        result = self.scan({}, ifaces=())
        self.assertEqual(result["error"], main.NO_NETWORK)

    def test_public_network_is_not_swept(self):
        result = self.scan({}, ifaces=("8.8.8.8/24",))
        self.assertEqual(result["error"], main.NOT_PRIVATE)

    def test_finds_new_devices_and_refreshes_saved_ips(self):
        main.save_machines([{"name": "Saved", "mac": MAC, "ip": "192.168.1.99"}])
        result = self.scan(
            {"192.168.1.10": MAC, "192.168.1.20": "AA:AA:AA:AA:AA:AA", "192.168.1.21": "AA:AA:AA:AA:AA:AA"},
            names={"192.168.1.21": "OTHER"},
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["devices"], [{"ip": "192.168.1.21", "mac": "AA:AA:AA:AA:AA:AA", "name": "OTHER"}])
        self.assertEqual(result["machines"], [{"name": "Saved", "mac": MAC, "ip": "192.168.1.10"}])

    def test_machine_added_during_scan_is_kept(self):
        # Simulates "Add by MAC" while the scan is waiting on reverse DNS
        added = lambda: main.save_machines([{"name": "Manual", "mac": "BB:BB:BB:BB:BB:BB", "ip": None}])
        result = self.scan({"192.168.1.10": "BB:BB:BB:BB:BB:BB"}, on_dns=added)
        self.assertEqual(result["devices"], [])
        self.assertEqual(result["machines"], [{"name": "Manual", "mac": "BB:BB:BB:BB:BB:BB", "ip": "192.168.1.10"}])


if __name__ == "__main__":
    unittest.main()
