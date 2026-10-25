#!/usr/bin/env bash
# Builds the plugin and produces out/Decky-WoL.zip for Decky's "Install Plugin From URL/ZIP"
set -euo pipefail
cd "$(dirname "$0")"

npm install
npm run build

rm -rf out
mkdir -p out/Decky-WoL
cp -r dist main.py plugin.json package.json README.md LICENSE out/Decky-WoL/
python3 -c 'import shutil; shutil.make_archive("out/Decky-WoL", "zip", "out", "Decky-WoL")'
echo "Built out/Decky-WoL.zip"
