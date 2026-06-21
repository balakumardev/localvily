#!/usr/bin/env bash
# Package the Chrome extension into a zip for the Web Store / manual install.
set -euo pipefail
cd "$(dirname "$0")/.."
npm run build:inject          # ensure generated inject bundles are current
OUT="dist/browser-relay-extension.zip"
mkdir -p dist
rm -f "$OUT"
( cd extension && zip -r "../$OUT" . -x '*/tests/*' 'tests/*' )
echo "wrote $OUT"
