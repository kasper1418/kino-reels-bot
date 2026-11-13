"""
Botsiz, to'g'ridan-to'g'ri serverda sinash uchun:

    python cli.py kino.mp4 --soni 10 --uzunlik 40 --matn "Kino nomi" --chiqish natija/
"""
import argparse
import time
from pathlib import Path

import analyze
import render


def main():
    p = argparse.ArgumentParser(description="Kinodan reels variantlar yasash")
    p.add_argument("kino")
    p.add_argument("--soni", type=int, default=10, help="nechta reels (standart 10)")
    p.add_argument("--uzunlik", type=int, default=40, help="har biri necha soniya (standart 40)")
    p.add_argument("--matn", default=None, help="tepaga yoziladigan matn")
    p.add_argument("--watermark", default=None, help="pastga yoziladigan kanal nomi")
    p.add_argument("--zoom", type=float, default=1.0, help="kadrni kattalashtirish (1.0–1.6)")
    p.add_argument("--chiqish", default="natija")
    a = p.parse_args()

    t0 = time.time()
    res = analyze.find_highlights(a.kino, a.soni, a.uzunlik, progress=lambda s: print("•", s))
    print(f"Tahlil: {time.time() - t0:.0f} s. Qora chiziqlar: {res.info.crop or 'yo‘q'}")

    out_dir = Path(a.chiqish)
    for c in res.clips:
        t = time.time()
        out = out_dir / f"reel_{c.rank:02d}.mp4"
        render.render_clip(res.info, c, out, a.matn, a.watermark, zoom=a.zoom)
        print(f"{c.rank:2d}. {analyze.fmt_time(c.start)}–{analyze.fmt_time(c.end)} "
              f"ball={c.score:.3f}  → {out}  ({time.time() - t:.0f} s)")
    for f in out_dir.glob("*.txt"):
        f.unlink()
    print(f"Hammasi: {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
