#!/usr/bin/env bash
# Adapted from pwa-starter's scripts/make-og.sh (af148d7); not a stamped copy, since check-downstream.py does not track that script.
# Rasterize the share card src/og.svg -> web/og.png (1200x630). The OG image MUST be a raster served
# at an ABSOLUTE https URL (iMessage/WhatsApp/Slack reject relative paths and won't render SVG), and
# must be compressed -- this script hard-fails if the card lands over MAX_BYTES (below), a margin
# under WhatsApp's ~300 KB scrape cutoff, since a too-big card previews as a grey box. Edit
# src/og.svg, rerun this. The card has live <text>, so the font must be installed locally or it
# falls back to a stock serif in the render. Needs rsvg-convert and pngquant (brew install librsvg
# pngquant). tests/test_pwa.py re-checks the size and dimensions of the committed file.
set -euo pipefail
cd "$(dirname "$0")/.."
svg=src/og.svg
png=web/og.png
rsvg-convert -w 1200 -h 630 "$svg" -o "$png"
# Compress: palette-quantize to shave the file -- scrapers skip an oversized card.
if command -v pngquant >/dev/null; then
  pngquant --force --skip-if-larger --output "$png" "$png" 2>/dev/null || true
fi

# Hard gate. A share card too big to scrape previews as a silent grey box, so FAIL instead of
# shipping it. MAX_BYTES keeps a margin under WhatsApp's ~300 KB cutoff (keep in sync with
# tests/test_pwa.py).
MAX_BYTES=250000
bytes=$(wc -c < "$png" | tr -d ' ')
if [ "$bytes" -gt "$MAX_BYTES" ]; then
  echo "ERROR: $png is $bytes bytes (> $MAX_BYTES). Install pngquant, simplify $svg, or shrink the palette." >&2
  exit 1
fi
echo "wrote $png ($bytes bytes)"
