"""画一个 d20（二十面骰）做程序图标。

为什么不用图片素材：这样图标是**算出来的**，改颜色改尺寸都是一行代码，
而且 16×16 到 256×256 每一档都是同一套几何，缩到多小都不会糊成一团。

几何：正二十面体正面看过去，外轮廓是一个正六边形，
中间那个朝上的等边三角形是正对我们的那一面，另外三个三角形是侧面。
所以画四条边就够——这是 d20 最容易被认出来的角度。

用法：<venv>\\Scripts\\python.exe tools\\make_icon.py
产物：assets\\icon.ico（多尺寸）+ assets\\icon.png（512，给 README 用）
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"

# 配色跟界面一致：深底 + 冷蓝
BG_TOP = (26, 34, 48)
BG_BOTTOM = (10, 14, 20)
ACCENT = (110, 168, 254)
INK = (18, 26, 38)

# 四个可见面的明暗（上方来光）。
# 暗面不能太暗——图标缩到 16×16 时，太暗的面会直接融进深色底里。
FACE_FRONT = (244, 248, 253)
FACE_UPLEFT = (206, 219, 234)
FACE_UPRIGHT = (186, 203, 224)
FACE_BOTTOM = (152, 170, 196)

SIZE = 1024            # 先画大图，最后再降采样，边缘才干净
SS = 2                 # 超采样倍数（抗锯齿）


def _hexagon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """外轮廓：正六边形，顶点从正上方开始逆时针。"""
    out = []
    for k in range(6):
        a = math.radians(90 + 60 * k)
        out.append((cx + r * math.cos(a), cy - r * math.sin(a)))
    return out


def _font(px: int) -> ImageFont.FreeTypeFont:
    for name in ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf",
                 "impact.ttf", "consolab.ttf", "msyhbd.ttc"):
        p = Path("C:/Windows/Fonts") / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), px)
            except Exception:
                continue
    return ImageFont.load_default()


def _vgradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g.resize((size, size))


def _rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1),
                                        radius=radius, fill=255)
    return m


def _shade(poly, c1, c2):
    """给一个多边形填上竖直渐变，做出"面"的立体感。

    返回的是**裁剪到包围盒**的图层，调用方要用带偏移的 alpha_composite 贴回去。
    """
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)) + 2, int(max(ys)) + 2
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = layer.load()
    for y in range(h):
        t = y / max(1, h - 1)
        col = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = (*col, 255)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon([(p[0] - x0, p[1] - y0) for p in poly], fill=255)
    layer.putalpha(mask)
    return layer, (x0, y0)


def draw_icon(size: int = SIZE) -> Image.Image:
    W = size * SS
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # ── 底：圆角深色 + 一点中心辉光 ──
    bg = _vgradient(W, BG_TOP, BG_BOTTOM).convert("RGBA")
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (W * 0.20, W * 0.20, W * 0.80, W * 0.80), fill=(*ACCENT, 74))
    glow = glow.filter(ImageFilter.GaussianBlur(W * 0.075))
    bg.alpha_composite(glow)
    img = Image.alpha_composite(img, bg)
    img.putalpha(_rounded_mask(W, int(W * 0.205)))

    cx = cy = W / 2
    R = W * 0.352
    P = _hexagon(cx, cy, R)
    P0, P1, P2, P3, P4, P5 = P

    faces = [
        # (顶点, 上色, 下色)
        ([P1, P0, P2], FACE_UPLEFT, tuple(int(c * 0.78) for c in FACE_UPLEFT)),
        ([P5, P4, P0], FACE_UPRIGHT, tuple(int(c * 0.80) for c in FACE_UPRIGHT)),
        ([P2, P3, P4], FACE_BOTTOM, tuple(int(c * 0.74) for c in FACE_BOTTOM)),
        ([P0, P2, P4], FACE_FRONT, tuple(int(c * 0.86) for c in FACE_FRONT)),
    ]
    for poly, c1, c2 in faces:
        layer, pos = _shade(poly, c1, c2)
        img.alpha_composite(layer, pos)

    # ── 棱线：深色描边 + 上方一道冷蓝反光 ──
    d = ImageDraw.Draw(img)
    lw = max(2, int(W * 0.0115))
    edges = [
        (P0, P1), (P1, P2), (P2, P3), (P3, P4), (P4, P5), (P5, P0),
        (P0, P2), (P0, P4), (P2, P4),
    ]
    for a, b in edges:
        d.line([a, b], fill=(*INK, 235), width=lw, joint="curve")

    rim = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rim)
    for a, b in [(P1, P0), (P0, P5), (P0, P2), (P0, P4)]:
        rd.line([a, b], fill=(*ACCENT, 130), width=max(1, lw // 3))
    img.alpha_composite(rim.filter(ImageFilter.GaussianBlur(W * 0.004)))

    # ── 正面那个 "20" ──
    d = ImageDraw.Draw(img)
    f = _font(int(R * 0.80))
    box = d.textbbox((0, 0), "20", font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    # 视觉重心略高于几何中心
    tx, ty = cx - tw / 2 - box[0], cy - th / 2 - box[1] - R * 0.03

    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((tx + W * 0.004, ty + W * 0.006), "20",
                                font=f, fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(W * 0.006)))
    ImageDraw.Draw(img).text((tx, ty), "20", font=f, fill=(*INK, 255))

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw_icon(SIZE)
    png = OUT / "icon.png"
    big.save(png)

    ico = OUT / "icon.ico"
    big.save(ico, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])

    print(f"已生成 {png}（512×512）")
    print(f"已生成 {ico}")
    for p in (png, ico):
        print(f"  {p.name}: {p.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
