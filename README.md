# Decky WoL

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that wakes your PC from the Steam Machine / Steam Deck Quick Access menu with one tap.

- **Scan network** finds devices on your LAN (by ARP, so it works even when the PC's firewall blocks ping) and names Windows PCs via NetBIOS / reverse DNS. Tap one to add it.
- **Add by MAC address** if the PC is off and can't be scanned.
- Each saved PC is a single button: tap it to send the magic packet.
- **Edit list** to remove machines.

## Install

1. Decky → Settings → General → enable **Developer mode**.
2. Decky → Settings → Developer → **Install Plugin From URL**, and enter:

   ```
   https://github.com/Picardas/Decky-WoL/releases/latest/download/Decky-WoL.zip
   ```

3. Open the Quick Access menu → **Wake on LAN**.

To update, install from the same URL again.

## PC setup (one-time)

- **BIOS/UEFI**: enable *Wake on LAN* / *Power On By PCI-E*; disable *ErP / deep sleep* if present.
- **Network adapter** (Device Manager → adapter → Advanced): *Wake on Magic Packet* = Enabled, *Shutdown Wake-On-Lan* = Enabled. Power Management tab: allow this device to wake the computer.
- **Fast Startup** (Control Panel → Power Options → Choose what the power buttons do): turn it off if waking from a full shutdown doesn't work. Waking from sleep is unaffected.
- Use **Ethernet** on the PC. Wake-on-LAN over Wi-Fi rarely works.

## Development

Needs Node.js 18+ and Python 3 (Linux or WSL).

```bash
./package.sh
```

This builds `dist/index.js` and packages `out/Decky-WoL.zip`, which you can also install with Decky's **Install Plugin From ZIP File**.

- `main.py`: backend (network scan, magic packet, saved machines)
- `src/index.tsx`: Quick Access panel

### Releasing

Bump `version` in `package.json`, commit, then tag and push:

```bash
git tag v1.0.1
git push origin v1.0.1
```

The [Release workflow](.github/workflows/release.yml) builds the zip and publishes it as the latest GitHub release, which the install URL above always points to.

## License

[BSD-3-Clause](LICENSE)
