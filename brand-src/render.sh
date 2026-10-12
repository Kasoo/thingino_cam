#!/bin/sh
# Render the brand/ PNGs from icon.svg. Needs inkscape. No logo: HA shows the icon in its place.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/../custom_components/thingino_cam/brand"
inkscape "$HERE/icon.svg" -w 256 -h 256 -o "$OUT/icon.png"
inkscape "$HERE/icon.svg" -w 512 -h 512 -o "$OUT/icon@2x.png"
