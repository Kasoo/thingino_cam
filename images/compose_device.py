"""Lay out a Home Assistant device-page screenshot compactly, with its cards in three columns.

    python3 compose_device.py SCREENSHOT OUT.png [--layout 'a c / b e / d'] [--blur CARD:x0,y0,x1,y1 ...]

Run it on a full-page screenshot of a device page in the dark theme. Cards are found against the page
background; the sidebar, the Related card and the Activity column are left out. Cards are lettered in page
order: `a` is Device info, then `b`, `c`, ... are the cards of the entity column from the top. Columns in
--layout are separated by `/`. Without it, each card goes to the currently shortest column. --blur
(repeatable) blurs a box, in pixels relative to the card, e.g. a MAC address or a live-view thumbnail.
"""
import argparse
import string

from PIL import Image, ImageFilter

BG = (17, 17, 17)
GAP, PAD = 16, 16
ROW = 200               # a pixel row that crosses the first row of cards


def is_bg(pixel):
    return sum(abs(a - b) for a, b in zip(pixel, BG)) < 6


def runs(im, fixed, vertical, start=0):
    """(start, end) spans longer than 30 px of non-background pixels along a column or row."""
    out, begin = [], None
    length = im.height if vertical else im.width
    for pos in range(start, length):
        bg = is_bg(im.getpixel((fixed, pos) if vertical else (pos, fixed)))
        if not bg and begin is None:
            begin = pos
        if bg and begin is not None:
            if pos - begin > 30:
                out.append((begin, pos))
            begin = None
    if begin is not None:
        out.append((begin, length))
    return out


def find_cards(im):
    columns = runs(im, ROW, vertical=False)[1:]         # skip the sidebar
    (ix0, ix1), (ex0, ex1) = columns[0], columns[1]
    info = runs(im, (ix0 + ix1) // 2, vertical=True, start=100)[0]
    entities = runs(im, (ex0 + ex1) // 2, vertical=True, start=100)
    cards = [im.crop((ix0, info[0], ix1, info[1]))]
    cards += [im.crop((ex0, top, ex1, bottom)) for top, bottom in entities]
    return dict(zip(string.ascii_lowercase, cards))


def blur(card, box, radius=6):
    card.paste(card.crop(box).filter(ImageFilter.GaussianBlur(radius)), box)


def masonry(cards, columns=3):
    """Each card, in page order, goes to the currently shortest column."""
    layout, heights = [[] for _ in range(columns)], [0] * columns
    for name, card in cards.items():
        i = heights.index(min(heights))
        layout[i].append(name)
        heights[i] += card.height + GAP
    return layout


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src")
    parser.add_argument("dst")
    parser.add_argument("--layout", help="card letters per column, e.g. 'a c / b e / d'")
    parser.add_argument("--blur", action="append", default=[], metavar="CARD:x0,y0,x1,y1")
    args = parser.parse_args()

    cards = find_cards(Image.open(args.src).convert("RGB"))
    print("cards:", {name: card.size for name, card in cards.items()})
    for spec in args.blur:
        name, _, box = spec.partition(":")
        blur(cards[name], tuple(int(v) for v in box.split(",")))

    layout = [col.split() for col in args.layout.split("/")] if args.layout else masonry(cards)
    width = cards["a"].width
    height = max(sum(cards[n].height for n in col) + GAP * (len(col) - 1) for col in layout)
    out = Image.new("RGB", (PAD * 2 + width * len(layout) + GAP * (len(layout) - 1), PAD * 2 + height), BG)
    for i, col in enumerate(layout):
        y = PAD
        for name in col:
            out.paste(cards[name], (PAD + i * (width + GAP), y))
            y += cards[name].height + GAP
    out.save(args.dst, optimize=True)
    print(args.dst, out.size)


if __name__ == "__main__":
    main()
