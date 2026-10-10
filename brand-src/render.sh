#!/bin/sh
# Render the brand/ PNGs from icon.svg and logo.svg. Needs inkscape.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/../custom_components/thingino_cam/brand"
inkscape "$HERE/icon.svg" -w 256 -h 256 -o "$OUT/icon.png"
inkscape "$HERE/icon.svg" -w 512 -h 512 -o "$OUT/icon@2x.png"
inkscape "$HERE/logo.svg" -h 128 -o "$OUT/logo.png"
inkscape "$HERE/logo.svg" -h 256 -o "$OUT/logo@2x.png"
