"""Generate the Grand Theft Semi-Auto YouTube profile picture and banner (original art, no Rockstar assets).

    python branding/make_branding.py   -> branding/profile.png (800x800), branding/banner.png (2560x1440)
                                          + *_preview.png with YouTube's crop guides
Uses Pillow and fonts that ship with Windows (Franklin Gothic Heavy Italic, Stencil, Impact).
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = Path("C:/Windows/Fonts")
SS = 2  # supersampling factor for smooth edges

# Leonida-sunset palette
SKY = [(0.00, (18, 8, 46)), (0.38, (66, 16, 98)), (0.62, (176, 32, 104)), (0.80, (255, 96, 72)),
       (1.00, (255, 176, 84))]
SUN_TOP, SUN_BOTTOM = (255, 226, 92), (255, 64, 140)
SILHOUETTE = (20, 6, 34)
NEON_PINK, NEON_CYAN = (255, 64, 160), (60, 230, 255)
WHITE = (255, 255, 255)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for cand in (name, name.lower(), name.upper()):
        p = FONTS / cand
        if p.exists():
            return ImageFont.truetype(str(p), size)
    raise SystemExit(f"Font {name} not found in {FONTS}")


def lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def gradient_stops(stops, t):
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return lerp(c0, c1, (t - t0) / (t1 - t0 or 1))
    return stops[-1][1]


def sky(w, h, horizon):
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):
        t = min(1.0, y / horizon)
        px.line([(0, y), (w, y)], fill=gradient_stops(SKY, t))
    return img


def stars(img, horizon, n, seed=7):
    import random
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    for _ in range(n):
        x, y = rnd.randrange(img.width), rnd.randrange(int(horizon * 0.55))
        r = rnd.choice([1, 1, 1, 2]) * SS
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 240, 255))


def retro_sun(img, cx, cy, r, horizon):
    """Gradient sun with synthwave stripe cut-outs, clipped at the horizon, with a soft glow."""
    w, h = img.size
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse([cx - r * 1.25, cy - r * 1.25, cx + r * 1.25, cy + r * 1.25], fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(r * 0.35))
    img.paste(Image.new("RGB", (w, h), (255, 120, 140)), mask=glow.point(lambda v: int(v * 0.6)))

    sun = Image.new("RGB", (w, h))
    sd = ImageDraw.Draw(sun)
    for y in range(int(cy - r), int(cy + r) + 1):
        sd.line([(cx - r, y), (cx + r, y)], fill=lerp(SUN_TOP, SUN_BOTTOM, (y - (cy - r)) / (2 * r)))
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    # stripes get thicker towards the bottom
    y, gap = cy + r * 0.05, r * 0.035
    while y < cy + r:
        md.rectangle([cx - r, y, cx + r, y + gap], fill=0)
        y += gap * 2.6
        gap *= 1.28
    md.rectangle([0, horizon, w, h], fill=0)
    img.paste(sun, mask=mask)


def grid_floor(img, horizon, vanish_x):
    w, h = img.size
    d = ImageDraw.Draw(img)
    d.rectangle([0, horizon, w, h], fill=(26, 6, 44))
    col = (255, 56, 150)
    for i in range(-24, 25):                      # converging lines
        x_far = vanish_x + i * w * 0.012
        x_near = vanish_x + i * w * 0.16
        d.line([(x_far, horizon), (x_near, h)], fill=col, width=2 * SS)
    k = 0.0                                       # horizontal lines, spaced like perspective
    while True:
        y = horizon + (h - horizon) * (k ** 2)
        if y > h:
            break
        d.line([(0, y), (w, y)], fill=col, width=2 * SS)
        k += 0.09
    d.line([(0, horizon), (w, horizon)], fill=(255, 150, 200), width=3 * SS)


def palm(d, bx, by, height, lean, s=1.0):
    """Silhouette palm tree: curved tapered trunk + drooping fronds."""
    pts_l, pts_r = [], []
    top = None
    for i in range(41):
        t = i / 40
        x = bx + lean * height * (t ** 2)
        y = by - height * t
        half = (16 - 9 * t) * s * SS
        pts_l.append((x - half, y))
        pts_r.append((x + half, y))
        top = (x, y)
    d.polygon(pts_l + pts_r[::-1], fill=SILHOUETTE)
    tx, ty = top
    for ang, length in [(-170, 1.0), (-140, 1.05), (-110, 0.8), (-70, 0.8), (-40, 1.05), (-10, 1.0),
                        (-90, 0.55), (160, 0.75), (20, 0.75)]:
        a = math.radians(ang)
        L = height * 0.42 * length
        upper, lower = [], []
        for j in range(21):
            t = j / 20
            droop = (t ** 2) * L * 0.55
            x = tx + math.cos(a) * L * t
            y = ty + math.sin(a) * L * t + droop
            wdt = math.sin(math.pi * t) * 20 * s * SS * length
            nx, ny = -math.sin(a), math.cos(a)
            upper.append((x + nx * wdt, y + ny * wdt * 0.5))
            lower.append((x - nx * wdt, y - ny * wdt * 0.5))
        d.polygon(upper + lower[::-1], fill=SILHOUETTE)


def semi_truck(d, x, ground, length, accent):
    """Side-view semi truck facing right; `length` = total length in px."""
    u = length / 100.0
    trailer_top = ground - 30 * u
    d.rounded_rectangle([x, trailer_top, x + 64 * u, ground - 7 * u], radius=2 * u, fill=SILHOUETTE)
    d.rectangle([x + 60 * u, ground - 9 * u, x + 70 * u, ground - 6 * u], fill=SILHOUETTE)    # hitch
    cab = [(x + 68 * u, ground - 6 * u), (x + 68 * u, ground - 31 * u), (x + 79 * u, ground - 31 * u),
           (x + 82 * u, ground - 20 * u), (x + 98 * u, ground - 18 * u), (x + 100 * u, ground - 14 * u),
           (x + 100 * u, ground - 6 * u)]
    d.polygon(cab, fill=SILHOUETTE)
    d.rectangle([x + 70 * u, ground - 37 * u, x + 72 * u, ground - 28 * u], fill=SILHOUETTE)   # exhaust
    d.polygon([(x + 75 * u, ground - 29 * u), (x + 79 * u, ground - 29 * u), (x + 81 * u, ground - 21 * u),
               (x + 75 * u, ground - 21 * u)], fill=accent)                                    # window glow
    d.rectangle([x + 98 * u, ground - 13 * u, x + 100 * u, ground - 11 * u], fill=(255, 230, 140))  # headlight
    d.line([(x + 2 * u, ground - 26 * u), (x + 62 * u, ground - 26 * u)], fill=accent, width=max(2, int(u * 0.8)))
    for wx in (8, 15, 54, 61, 77, 92):
        r = 4.6 * u
        d.ellipse([x + wx * u - r, ground - r * 1.3, x + wx * u + r, ground + r * 0.7], fill=SILHOUETTE)
        r2 = 1.7 * u
        d.ellipse([x + wx * u - r2, ground - r * 0.3 - r2, x + wx * u + r2, ground - r * 0.3 + r2], fill=accent)


def outlined_text(img, xy, text, fnt, fill, outline, stroke, shadow=0, anchor="mm", glow=None):
    if glow:
        layer = Image.new("L", img.size, 0)
        ImageDraw.Draw(layer).text(xy, text, font=fnt, fill=255, anchor=anchor, stroke_width=stroke * 2)
        layer = layer.filter(ImageFilter.GaussianBlur(stroke * 2.2))
        img.paste(Image.new("RGB", img.size, glow), mask=layer)
    d = ImageDraw.Draw(img)
    if shadow:
        d.text((xy[0] + shadow, xy[1] + shadow), text, font=fnt, fill=(0, 0, 0), anchor=anchor,
               stroke_width=stroke, stroke_fill=(0, 0, 0))
    d.text(xy, text, font=fnt, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=outline)


def fit_font(name, text, max_w, start):
    size = start
    while size > 10:
        f = font(name, size)
        if f.getlength(text) <= max_w:
            return f
        size -= 4
    return font(name, size)


# --------------------------------------------------------------------------- banner
def make_banner():
    W, H = 2560 * SS, 1440 * SS
    horizon = int(905 * SS)
    img = sky(W, H, horizon)
    stars(img, horizon, 260)
    retro_sun(img, W // 2, int(770 * SS), int(330 * SS), horizon)
    grid_floor(img, horizon, W // 2)
    d = ImageDraw.Draw(img)
    # palms framing the scene (left inside desktop band, right behind the truck)
    palm(d, 300 * SS, horizon + 12 * SS, 520 * SS, 0.18, 1.3)
    palm(d, 470 * SS, horizon + 12 * SS, 390 * SS, -0.22, 1.05)
    palm(d, 2450 * SS, horizon + 12 * SS, 560 * SS, -0.2, 1.35)
    palm(d, 140 * SS, horizon + 12 * SS, 300 * SS, 0.3, 0.9)
    semi_truck(d, 1840 * SS, horizon + 4 * SS, 470 * SS, NEON_CYAN)

    # title inside YouTube's all-devices safe area: 1546x423 centred
    cx = W // 2
    title_f = fit_font("FRAHVIT.TTF", "GRAND THEFT", 1150 * SS, 200 * SS)
    semi_f = fit_font("STENCIL.TTF", "SEMI-AUTO", 1100 * SS, 190 * SS)
    tag_f = font("impact.ttf", 46 * SS)
    outlined_text(img, (cx, 612 * SS), "GRAND THEFT", title_f, WHITE, SILHOUETTE, 10 * SS, shadow=10 * SS,
                  glow=(120, 20, 120))
    outlined_text(img, (cx, 772 * SS), "SEMI-AUTO", semi_f, (255, 214, 64), SILHOUETTE, 10 * SS, shadow=10 * SS,
                  glow=(140, 30, 60))
    outlined_text(img, (cx, 892 * SS), "GTA STREAMER CLIPS  •  NEW SHORTS DAILY", tag_f, WHITE, SILHOUETTE,
                  5 * SS)
    img = img.resize((2560, 1440), Image.LANCZOS)
    img.save(HERE / "banner.png", optimize=True)

    # preview with YouTube crop guides
    prev = img.copy()
    pd = ImageDraw.Draw(prev)
    pd.rectangle([0, 508, 2559, 931], outline=(0, 255, 0), width=4)          # desktop band
    pd.rectangle([507, 508, 2052, 931], outline=(255, 255, 0), width=4)      # all-devices safe area
    pd.text((520, 520), "safe area (all devices)", fill=(255, 255, 0), font=font("arialbd.ttf", 32))
    pd.text((20, 520), "desktop", fill=(0, 255, 0), font=font("arialbd.ttf", 32))
    prev.save(HERE / "banner_preview.png")


# --------------------------------------------------------------------------- profile picture
def make_profile():
    S = 800 * SS
    horizon = int(560 * SS)
    img = sky(S, S, horizon)
    stars(img, horizon, 40, seed=3)
    retro_sun(img, S // 2, int(400 * SS), int(255 * SS), horizon)
    grid_floor(img, horizon, S // 2)
    d = ImageDraw.Draw(img)
    palm(d, 115 * SS, horizon + 8 * SS, 330 * SS, 0.25, 1.0)
    palm(d, 690 * SS, horizon + 8 * SS, 300 * SS, -0.25, 0.95)
    semi_truck(d, 255 * SS, horizon + 3 * SS, 290 * SS, NEON_CYAN)
    f = fit_font("FRAHVIT.TTF", "GTSA", 560 * SS, 250 * SS)
    outlined_text(img, (S // 2, 335 * SS), "GTSA", f, WHITE, SILHOUETTE, 12 * SS, shadow=12 * SS, glow=(120, 20, 120))
    img = img.resize((800, 800), Image.LANCZOS)
    img.save(HERE / "profile.png", optimize=True)

    # preview: how YouTube shows it (circle) at large and tiny sizes
    mask = Image.new("L", (800, 800), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 799, 799], fill=255)
    circ = Image.new("RGB", (800, 800), (15, 15, 15))
    circ.paste(img, mask=mask)
    prev = Image.new("RGB", (800 + 40 + 98 + 40 + 48 + 40, 800), (15, 15, 15))
    prev.paste(circ, (0, 0))
    for size, x in ((98, 840), (48, 978)):
        small = circ.resize((size, size), Image.LANCZOS)
        prev.paste(small, (x, 400 - size // 2))
    prev.save(HERE / "profile_preview.png")


if __name__ == "__main__":
    make_banner()
    make_profile()
    print("wrote", HERE / "banner.png", "and", HERE / "profile.png")
