# Decky WoL

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that wakes your PC from the Steam Machine / Steam Deck Quick Access menu with one tap.

- **Scan network** finds devices on your LAN (by ARP, so it works even when the PC's firewall blocks ping) and names Windows PCs via NetBIOS / reverse DNS. Tap one to add it. Only private home networks are scanned (never public-IP, VPN or container networks), at most the /24 around the Deck.
- **Add by MAC address** if the PC is off and can't be scanned.
- Each saved PC is a single button: tap it to send the magic packet.
- Opening the plugin puts focus straight on the top PC's wake button, so waking it is a single press of **A**.
- After a successful wake the Quick Access menu closes itself. If sending fails it stays open and shows why.
- No pop-up notifications: errors show inline in the panel.
- To keep Wake on LAN at the top of Decky's list: Decky settings → **Plugins**, highlight Wake on LAN, press **X** (Reorder), move it up with the D-pad, then press **X** again to save. *Sort plugins alphabetically* (Decky settings → General) must be off.
- **Edit list** to remove machines.
- Saved PCs live in `machines.json` in the plugin's folder under `~/homebrew/settings/` and are kept if you uninstall, so a reinstall picks them up again. If that file is ever damaged, it is moved aside to `machines.json.corrupt-<date>` rather than overwritten.

## Install

1. Decky → Settings → General → enable **Developer mode**.
2. Decky → Settings → Developer → **Install Plugin From URL**, and enter:

   ```
   https://github.com/Picardas/Decky-WoL/releases/latest/download/Decky-WoL.zip
   ```

3. Open the Quick Access menu → **Wake on LAN**.

To update, install from the same URL again. Each release also publishes `Decky-WoL.zip.sha256` if you want to check the download.

## PC setup (one-time)

- **BIOS/UEFI**: enable *Wake on LAN* / *Power On By PCI-E*; disable *ErP / deep sleep* if present.
- **Network adapter** (Device Manager → adapter → Advanced): *Wake on Magic Packet* = Enabled, *Shutdown Wake-On-Lan* = Enabled. Power Management tab: allow this device to wake the computer.
- **Fast Startup** (Control Panel → Power Options → Choose what the power buttons do): turn it off if waking from a full shutdown doesn't work. Waking from sleep is unaffected.
- Use **Ethernet** on the PC. Wake-on-LAN over Wi-Fi rarely works.

## Development

Laid out like the official [decky-plugin-template](https://github.com/SteamDeckHomebrew/decky-plugin-template). Needs Node.js 18+, [pnpm](https://pnpm.io) v9 (`npm i -g pnpm@9`) and Python 3 (Linux or WSL).

```bash
./package.sh
```

This builds `dist/index.js` and packages `out/Decky-WoL.zip` (plus its `.sha256`), which you can also install with Decky's **Install Plugin From ZIP File**.

Run the backend tests with `pnpm test` (plain `unittest`, no extra packages). CI runs the typecheck, build and tests on every push and pull request.

- `main.py`: backend (network scan, magic packet, saved machines)
- `src/index.tsx`: Quick Access panel
- `decky.pyi`: type stubs for the `decky` backend module
- `tests/`: backend tests (the `decky` module is stubbed)
- `.vscode/`: the template's build/deploy tasks (Decky CLI build, copy to your Deck over SSH). The deploy tasks expect the Decky CLI's `out/Wake on LAN.zip`, not `package.sh`'s `out/Decky-WoL.zip`.

### Releasing

Bump `version` in `package.json`, commit, then tag and push:

```bash
git tag v1.1.0
git push origin v1.1.0
```

The [Release workflow](.github/workflows/release.yml) checks the tag matches `package.json`, runs the tests, builds the zip and publishes it (with its checksum) as the latest GitHub release, which the install URL above always points to.

## License

[BSD-3-Clause](LICENSE)
