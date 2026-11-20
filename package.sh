#!/usr/bin/env bash
# Builds the plugin and produces out/Decky-WoL.zip for Decky's "Install Plugin From URL/ZIP"
set -euo pipefail
cd "$(dirname "$0")"

# No dependency needs install scripts; skipping them keeps a compromised package from running code here
pnpm install --frozen-lockfile --ignore-scripts
pnpm run build

rm -rf out
mkdir -p out/Decky-WoL/dist
cp dist/index.js out/Decky-WoL/dist/
cp main.py plugin.json package.json README.md LICENSE out/Decky-WoL/
python3 -c 'import shutil; shutil.make_archive("out/Decky-WoL", "zip", "out", "Decky-WoL")'
# Checksum to publish next to the zip (Python, since macOS has no sha256sum)
python3 -c 'import hashlib; print(hashlib.sha256(open("out/Decky-WoL.zip", "rb").read()).hexdigest() + "  Decky-WoL.zip")' > out/Decky-WoL.zip.sha256
echo "Built out/Decky-WoL.zip"
