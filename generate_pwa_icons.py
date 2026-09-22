"""Generate the PWA icon set for the Maorif Portal.

Renders a clean navy/gold "М" mark matching the portal branding
(#003366 navbar, #ffd700 accent) into portal/static/portal/icons/.

    python generate_pwa_icons.py

Pure-Pillow, no external artwork. Re-run any time the branding changes.
"""
import os

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, 'portal', 'static', 'portal', 'icons')

NAVY = (0, 51, 102, 255)   # #003366 — portal navbar/footer
GOLD = (255, 215, 0, 255)  # #ffd700 — portal accent
LETTER = 'М'               # Маориф

FONT_CANDIDATES = [
    r'C:\Windows\Fonts\arialbd.ttf',   # Arial Bold (Cyrillic support)
    r'C:\Windows\Fonts\segoeuib.ttf',  # Segoe UI Bold
    'DejaVuSans-Bold.ttf',
]


def _load_font(px):
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, px)
        except (OSError, IOError):
            continue
    raise RuntimeError('No usable bold font found for icon rendering')


def render(path, size, rounded=True, letter_ratio=0.55):
    """Render one icon. Supersampled 4x for clean antialiasing.

    rounded=True -> transparent corners, for 'any' purpose icons.
    rounded=False -> full-bleed square, for maskable/Apple icons where the
    OS applies its own mask (content stays inside the safe zone).
    """
    s = size * 4
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=NAVY)
    else:
        d.rectangle([0, 0, s - 1, s - 1], fill=NAVY)
    font = _load_font(int(s * letter_ratio))
    bbox = d.textbbox((0, 0), LETTER, font=font)
    x = (s - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (s - (bbox[3] - bbox[1])) / 2 - bbox[1]
    d.text((x, y), LETTER, font=font, fill=GOLD)
    img.resize((size, size), Image.LANCZOS).save(path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    render(os.path.join(OUT_DIR, 'icon-192.png'), 192)
    render(os.path.join(OUT_DIR, 'icon-512.png'), 512)
    render(os.path.join(OUT_DIR, 'icon-maskable-512.png'), 512,
           rounded=False, letter_ratio=0.42)
    render(os.path.join(OUT_DIR, 'apple-touch-icon.png'), 180,
           rounded=False, letter_ratio=0.5)
    render(os.path.join(OUT_DIR, 'favicon-32.png'), 32,
           rounded=False, letter_ratio=0.55)
    print('Wrote icons to', OUT_DIR)


if __name__ == '__main__':
    main()
