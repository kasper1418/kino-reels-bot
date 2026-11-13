"""
Reelsning yuqori qismiga kino kodi va matnni chiroyli qilib chizadi (shaffof PNG).

Matnda *yulduzcha* ichiga olingan so'zlar rangli (sariq) bo'ladi:
    Eng *kuchli* jang sahnasi   →   "kuchli" so'zi sariq
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
WHITE = (255, 255, 255, 255)
ACCENT = (255, 214, 10, 255)       # ajratilgan so'zlar va kod belgisi rangi
BLACK = (0, 0, 0, 255)
SIDE_MARGIN = 60

_FONT_GLOBS = [
    "/usr/share/fonts/**/Montserrat-ExtraBold.*",
    "/usr/share/fonts/**/Montserrat-Bold.*",
    "/usr/share/fonts/**/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def find_font() -> str | None:
    env = os.getenv("FONT_PATH", "")
    if env and os.path.exists(env):
        return env
    for pattern in _FONT_GLOBS:
        hits = sorted(glob.glob(pattern, recursive=True))
        for h in hits:
            if h.lower().endswith((".ttf", ".otf")):
                return h
    return None


def _clean(text: str) -> str:
    text = (text or "").replace("ʻ", "'").replace("ʼ", "'").replace("’", "'").replace("‘", "'").replace("`", "'")
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF)     # emoji shriftda yo'q
    return re.sub(r"[ \t]+", " ", text).strip()


def _parse(text: str) -> list[tuple[str, bool]]:
    """Matnni (so'z, ajratilganmi) ro'yxatiga aylantiradi."""
    words: list[tuple[str, bool]] = []
    parts = re.split(r"\*(.+?)\*", text)           # toq indekslar — yulduzcha ichidagilar
    for i, part in enumerate(parts):
        accent = i % 2 == 1
        for w in part.replace("*", "").split():
            words.append((w, accent))
    return words


def _wrap(draw, words, font, max_w):
    space = draw.textlength(" ", font=font)
    lines: list[list[tuple[str, bool, float]]] = []
    cur: list[tuple[str, bool, float]] = []
    cur_w = 0.0
    for w, acc in words:
        ww = draw.textlength(w, font=font)
        if cur and cur_w + space + ww > max_w:
            lines.append(cur)
            cur, cur_w = [(w, acc, ww)], ww
        else:
            cur_w += (space if cur else 0) + ww
            cur.append((w, acc, ww))
    if cur:
        lines.append(cur)
    return lines, space


def make_overlay(title: str | None, code: str | None, top_h: int, out: Path) -> Path | None:
    """Yuqori qora joy balandligi top_h ga moslab, matn va kod PNG sini yaratadi."""
    title, code = _clean(title or ""), _clean(code or "")
    if not title and not code:
        return None
    font_path = find_font()
    if not font_path:
        return None

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    max_w = W - 2 * SIDE_MARGIN

    # Matn bloki joylashadigan oraliq: yuqori qora joy ichida (Instagram yuqori paneli ostida)
    if top_h >= 420:
        y0, y1 = 190, top_h - 40
    else:                                   # tik video: yuqorida, rasm ustida
        y0, y1 = 120, 120 + 360
    region_h = max(120, y1 - y0)

    words = _parse(title)
    pill_text = f"KINO KODI: {code}" if code else ""
    gap = 28

    chosen = None
    for size in range(88, 35, -4):
        font = ImageFont.truetype(font_path, size)
        lines, space = _wrap(d, words, font, max_w) if words else ([], 0)
        if any(sum(w[2] for w in ln) + space * (len(ln) - 1) > max_w for ln in lines):
            continue
        line_h = int(size * 1.22)
        text_h = line_h * len(lines)
        pill_size = max(32, int(size * 0.55))
        pill_h = int(pill_size * 1.0) + 36 if pill_text else 0
        block_h = text_h + (pill_h + (gap if lines else 0) if pill_text else 0)
        if len(lines) <= 4 and block_h <= region_h:
            chosen = (font, lines, space, line_h, pill_size, pill_h, block_h)
            break
    if chosen is None:                      # sig'magan bo'lsa, eng kichik shrift
        font = ImageFont.truetype(font_path, 36)
        lines, space = _wrap(d, words, font, max_w) if words else ([], 0)
        lines = lines[:5]
        line_h = int(36 * 1.22)
        pill_size = 32
        pill_h = 68 if pill_text else 0
        chosen = (font, lines, space, line_h, pill_size, pill_h,
                  line_h * len(lines) + (pill_h + gap if pill_text and lines else pill_h))

    font, lines, space, line_h, pill_size, pill_h, block_h = chosen
    y = y0 + max(0, (region_h - block_h) // 2)

    if pill_text:                           # kino kodi: sariq yumaloq belgi
        pf = ImageFont.truetype(font_path, pill_size)
        while d.textlength(pill_text, font=pf) > max_w - 72 and pill_size > 22:
            pill_size -= 2
            pf = ImageFont.truetype(font_path, pill_size)
        tw = d.textlength(pill_text, font=pf)
        pw = int(tw + 72)
        x = (W - pw) // 2
        d.rounded_rectangle([x, y, x + pw, y + pill_h], radius=pill_h // 2, fill=ACCENT)
        d.text((W / 2, y + pill_h / 2 + 1), pill_text, font=pf, fill=BLACK, anchor="mm")
        y += pill_h + gap

    for ln in lines:                        # matn: oq, ajratilgan so'zlar sariq, qora soyali
        total = sum(w[2] for w in ln) + space * (len(ln) - 1)
        x = (W - total) / 2
        for word, acc, ww in ln:
            col = ACCENT if acc else WHITE
            d.text((x + 3, y + 4), word, font=font, fill=(0, 0, 0, 170), stroke_width=4, stroke_fill=(0, 0, 0, 170))
            d.text((x, y), word, font=font, fill=col, stroke_width=3, stroke_fill=BLACK)
            x += ww + space
        y += line_h

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
