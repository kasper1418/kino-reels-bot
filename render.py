"""
Tanlangan oraliqni vertikal (1080x1920) reels videoga aylantirish.

Ko'rinish: o'rtada kino kadri, orqa fonda o'sha kadrning xiralashtirilgan
kattalashtirilgan nusxasi. Tepada ixtiyoriy sarlavha, pastda ixtiyoriy
kanal nomi (watermark). Ovoz telefon uchun normallashtiriladi.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from analyze import Clip, MediaInfo

OUT_W, OUT_H = 1080, 1920
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _fg_size(info: MediaInfo, zoom: float = 1.0) -> tuple[int, int, int, int]:
    """Markaziy kadr o'lchami: (masshtab_w, masshtab_h, ko'rinadigan_w, ko'rinadigan_h).

    zoom > 1 bo'lsa, kadr kattalashtiriladi va yon tomonlari kesiladi —
    telefonda kino kattaroq ko'rinadi.
    """
    dw, dh = info.display_size
    s = min(OUT_W / dw, OUT_H / dh)
    zoom = max(1.0, min(zoom, (OUT_H * 0.9) / (dh * s)))
    sw, sh = _even(dw * s * zoom), _even(dh * s * zoom)
    return sw, sh, min(OUT_W, sw), min(OUT_H, sh)


def _drawtext(textfile: Path, font: str, size: int, y: int, color: str = "white") -> str:
    return (
        f"drawtext=fontfile={font}:textfile={textfile}:expansion=none:"
        f"fontsize={size}:fontcolor={color}:borderw=4:bordercolor=black@0.85:"
        f"x=(w-text_w)/2:y={y}"
    )


def build_filter(info: MediaInfo, length: float, workdir: Path,
                 title: str | None, watermark: str | None, font: str,
                 zoom: float = 1.0) -> tuple[str, bool]:
    sw, sh, fw, fh = _fg_size(info, zoom)
    top_h = (OUT_H - fh) // 2

    pre = f"[0:{info.video_index}]"
    if info.crop:
        cw, ch, cx, cy = info.crop
        pre += f"crop={cw}:{ch}:{cx}:{cy},"
    pre += "split=2[b0][f0]"

    parts = [
        pre,
        f"[b0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},setsar=1,scale=216:384,gblur=sigma=6,"
        f"scale={OUT_W}:{OUT_H},eq=brightness=-0.12:saturation=1.15[bg]",
        f"[f0]scale={sw}:{sh},setsar=1" + (f",crop={fw}:{fh}" if (sw, sh) != (fw, fh) else "") + "[fg]",
    ]

    post = ["overlay=(W-w)/2:(H-h)/2"]
    has_font = bool(font) and os.path.exists(font)

    if title and has_font:
        lines = textwrap.wrap(title.strip(), width=20)[:3]
        size, gap = 62, 16
        block = len(lines) * size + (len(lines) - 1) * gap
        y = max(90, top_h - 70 - block)
        for i, line in enumerate(lines):
            tf = workdir / f"title_{i}.txt"
            tf.write_text(line, encoding="utf-8")
            post.append(_drawtext(tf, font, size, y + i * (size + gap)))

    if watermark and has_font:
        tf = workdir / "watermark.txt"
        tf.write_text(watermark.strip()[:40], encoding="utf-8")
        bottom_start = top_h + fh
        y = bottom_start + 60 if OUT_H - bottom_start > 200 else OUT_H - 160
        post.append(_drawtext(tf, font, 44, y, color="white@0.9"))

    fade_out = max(0.0, length - 0.6)
    post += [
        "fade=t=in:st=0:d=0.4",
        f"fade=t=out:st={fade_out:.2f}:d=0.6",
        "format=yuv420p",
    ]
    parts.append("[bg][fg]" + ",".join(post) + "[v]")

    has_audio = info.audio_index is not None
    if has_audio:
        parts.append(
            f"[0:{info.audio_index}]aformat=channel_layouts=stereo,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
            f"afade=t=in:st=0:d=0.3,afade=t=out:st={fade_out:.2f}:d=0.6[a]"
        )
    return ";".join(parts), has_audio


def render_clip(info: MediaInfo, clip: Clip, out_path: Path,
                title: str | None = None, watermark: str | None = None,
                font: str = DEFAULT_FONT, zoom: float = 1.0, timeout: int = 900) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    length = clip.duration
    fc, has_audio = build_filter(info, length, out_path.parent, title, watermark, font, zoom)

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{clip.start:.3f}", "-i", info.path, "-t", f"{length:.3f}",
        "-filter_complex", fc, "-map", "[v]",
    ]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi: {r.stderr.strip()[-500:]}")
    return out_path


def make_thumb(video: Path, at: float, out: Path) -> Path | None:
    r = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=180:320", "-q:v", "4", str(out),
    ], capture_output=True)
    return out if r.returncode == 0 and out.exists() else None
