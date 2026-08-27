"""Generate QR codes for the roster.

    python scripts/make_qr.py              # printable sheet + payload list
    python scripts/make_qr.py --list-only  # just the payloads, for keyboard testing

A USB scanner in HID mode types the payload and presses Enter, so the printed sheet
and typing a payload by hand are the same input as far as the kiosk is concerned.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trackify.core import db
from trackify.core.config import load_config
from trackify.core.qrcodes import encode

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "qr"
COLS, ROWS = 3, 4
CELL_W, CELL_H = 620, 460
QR_PX = 300


# Label fonts for the printed sheet, in preference order. arial.ttf resolves on Windows
# and nowhere else: on the Pi it raised OSError every time and fell through to
# ImageFont.load_default(), which is a small bitmap face. The QR codes still scanned, but
# the student name and payload printed under each one came out barely legible -- on the
# cards a person has to read to hand the right one to the right child.
LABEL_FONTS = (
    "arial.ttf",                                            # Windows
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      # Debian / Raspberry Pi OS
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",                                       # anywhere on the font path
)


def _label_fonts():
    """(name_font, meta_font) -- the first family that loads, at 30px and 22px.

    Pillow is imported here rather than at module scope so that --list-only keeps
    working on a machine without it, which is the whole reason main() imports it late.
    """
    from PIL import ImageFont

    for candidate in LABEL_FONTS:
        try:
            return (ImageFont.truetype(candidate, 30),
                    ImageFont.truetype(candidate, 22))
        except OSError:
            continue
    # Legible enough to prove the pipeline works, small enough to notice. Say so rather
    # than silently printing an unreadable sheet.
    print("\nNo scalable font found; card labels will be small. Install fonts-dejavu.",
          file=sys.stderr)
    return ImageFont.load_default(), ImageFont.load_default()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    config = load_config()
    if not config.secrets.qr_secret:
        print("TRACKIFY_QR_SECRET is not set.", file=sys.stderr)
        return 1

    conn = db.connect()
    students = conn.execute(
        """SELECT s.id, s.lrn, s.first_name, s.last_name,
                  sec.name AS section, sec.grade_level
           FROM students s JOIN sections sec ON sec.id = s.section_id
           WHERE s.active = 1 ORDER BY s.id"""
    ).fetchall()
    if not students:
        print("No students. Run: python scripts/seed_demo.py", file=sys.stderr)
        return 1

    # The payload carries the LRN, matching qr-generator.exe exactly. Two generators
    # that disagree about what goes in a code is the bug this replaced; a card printed
    # here and one printed there must be the same string.
    skipped = [r for r in students if not str(r["lrn"]).isdigit()]
    students = [r for r in students if str(r["lrn"]).isdigit()]

    print(f"\n{'LRN':<14} {'PAYLOAD':<26} NAME")
    print("-" * 72)
    for row in students:
        payload = encode(int(row["lrn"]), config.secrets.qr_secret)
        print(f"{row['lrn']:<14} {payload:<26} {row['first_name']} {row['last_name']}")
    print("-" * 72)

    for row in skipped:
        print(f"SKIPPED {row['first_name']} {row['last_name']}: "
              f"LRN {row['lrn']!r} is not numeric and cannot be signed.", file=sys.stderr)
    print("\nType any payload into the kiosk and press Enter -- identical to a scan.")
    print("Printing for a webcam: make each code at least 25 mm wide, and use")
    print("matte lamination -- glare on a glossy card is the usual reason one")
    print("will not read.")

    if args.list_only:
        return 0

    try:
        import qrcode
        from PIL import Image, ImageDraw
    except ImportError:
        print("\nqrcode/Pillow not installed; skipping image sheet.", file=sys.stderr)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name_font, meta_font = _label_fonts()

    pages, per_page = [], COLS * ROWS
    for start in range(0, len(students), per_page):
        chunk = students[start:start + per_page]
        sheet = Image.new("RGB", (COLS * CELL_W, ROWS * CELL_H), "white")
        draw = ImageDraw.Draw(sheet)

        for index, row in enumerate(chunk):
            col, line = index % COLS, index // COLS
            x, y = col * CELL_W, line * CELL_H

            payload = encode(int(row["lrn"]), config.secrets.qr_secret)
            qr = qrcode.QRCode(box_size=10, border=2,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            img = img.resize((QR_PX, QR_PX))
            sheet.paste(img, (x + (CELL_W - QR_PX) // 2, y + 30))

            name = f"{row['first_name']} {row['last_name']}"
            meta = f"{row['grade_level']}-{row['section']}"
            for text, font, offset in ((name, name_font, 350),
                                       (meta, meta_font, 392),
                                       (payload, meta_font, 424)):
                width = draw.textlength(text, font=font)
                draw.text((x + (CELL_W - width) / 2, y + offset), text,
                          fill="black", font=font)
            draw.rectangle([x + 6, y + 6, x + CELL_W - 6, y + CELL_H - 6],
                           outline="#cccccc")

        path = OUT_DIR / f"qr-sheet-{len(pages) + 1}.png"
        sheet.save(path)
        pages.append(path)

    print(f"\nWrote {len(pages)} sheet(s):")
    for path in pages:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
