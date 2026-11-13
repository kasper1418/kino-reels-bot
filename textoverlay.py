"""
Reelsning yuqori qismiga kino kodi va matnni chiroyli qilib chizadi (shaffof PNG).

Rangli so'zlar:  *so'z* — sariq,  [so'z] — qizil
    Eng *kuchli* jang [ayovsiz] sahnasi
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
WHITE = (255, 255, 255, 255)
ACCENT = (255, 214, 10, 255)       # sariq: *so'z*, kod belgisi
RED = (255, 69, 58, 255)           # qizil: [so'z]
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


def _parse(text: str) -> list[tuple[str, tuple | None]]:
    """Matnni (so'z, rang) ro'yxatiga aylantiradi. rang=None — oddiy oq so'z."""
    words: list[tuple[str, tuple | None]] = []
    pos = 0
    for m in re.finditer(r"\*(.+?)\*|\[(.+?)\]", text):
        for w in text[pos:m.start()].replace("*", "").replace("[", "").replace("]", "").split():
            words.append((w, None))
        if m.group(1) is not None:
            words += [(w, ACCENT) for w in m.group(1).split()]
        else:
            words += [(w, RED) for w in m.group(2).split()]
        pos = m.end()
    for w in text[pos:].replace("*", "").replace("[", "").replace("]", "").split():
        words.append((w, None))
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
    """Matn — tepadagi qora joyda, kino kodi — pastdagi qora joyda (kichik, qalin belgi).

    top_h: kadr ustidagi (va ostidagi) qora joy balandligi.
    """
    title, code = _clean(title or ""), _clean(code or "")
    if not title and not code:
        return None
    font_path = find_font()
    if not font_path:
        return None

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    max_w = W - 2 * SIDE_MARGIN

    # ---------------------------------------------------------------- tepada: matn
    words = _parse(title)
    if words:
        if top_h >= 420:                    # Instagram yuqori paneli ostidan, kadrgacha
            y0, y1 = 190, top_h - 40
        else:                               # tik video: yuqorida, rasm ustida
            y0, y1 = 120, 120 + 360
        region_h = max(120, y1 - y0)

        chosen = None
        for size in range(88, 35, -4):
            font = ImageFont.truetype(font_path, size)
            lines, space = _wrap(d, words, font, max_w)
            if any(sum(w[2] for w in ln) + space * (len(ln) - 1) > max_w for ln in lines):
                continue
            line_h = int(size * 1.22)
            if len(lines) <= 4 and line_h * len(lines) <= region_h:
                chosen = (font, lines, space, line_h)
                break
        if chosen is None:                  # sig'magan bo'lsa, eng kichik shrift
            font = ImageFont.truetype(font_path, 36)
            lines, space = _wrap(d, words, font, max_w)
            chosen = (font, lines[:5], space, int(36 * 1.22))

        font, lines, space, line_h = chosen
        y = y0 + max(0, (region_h - line_h * len(lines)) // 2)
        for ln in lines:                    # oq matn, ajratilgan so'zlar sariq, qora soyali
            total = sum(w[2] for w in ln) + space * (len(ln) - 1)
            x = (W - total) / 2
            for word, acc, ww in ln:
                col = acc or WHITE
                d.text((x + 3, y + 4), word, font=font, fill=(0, 0, 0, 170), stroke_width=4, stroke_fill=(0, 0, 0, 170))
                d.text((x, y), word, font=font, fill=col, stroke_width=3, stroke_fill=BLACK)
                x += ww + space
            y += line_h

    # ------------------------------------------------------------ pastda: kino kodi
    if code:
        size = 38                           # kichik, lekin qalin shrift
        pill_text = f"KINO KODI: {code}"
        pf = ImageFont.truetype(font_path, size)
        while d.textlength(pill_text, font=pf) > max_w - 60 and size > 22:
            size -= 2
            pf = ImageFont.truetype(font_path, size)
        tw = d.textlength(pill_text, font=pf)
        pw, ph = int(tw + 56), int(size + 30)
        if top_h >= 300:                    # kadr ostidagi qora joyda, kadrga yaqin
            py = H - top_h + 56
        else:                               # tik video: pastda, Instagram yozuvlaridan yuqoriroqda
            py = H - 420
        px = (W - pw) // 2
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=ph // 2, fill=ACCENT)
        d.text((W / 2, py + ph / 2 + 1), pill_text, font=pf, fill=BLACK, anchor="mm")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
