"""
Tanlangan oraliqni vertikal (1080x1920) reels videoga aylantirish.

Ko'rinish: o'rtada kino kadri, tepasi va pasti qora fon. Matn yo'q.
Ovoz telefon uchun normallashtiriladi.
"""
from __future__ import annotations

import os
import subprocess
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
    """Qora fon ustida markazda kino kadri. Matn yo'q (title/watermark e'tiborsiz)."""
    sw, sh, fw, fh = _fg_size(info, zoom)

    chain = f"[0:{info.video_index}]"
    if info.crop:
        cw, ch, cx, cy = info.crop
        chain += f"crop={cw}:{ch}:{cx}:{cy},"
    chain += f"scale={sw}:{sh},setsar=1"
    if (sw, sh) != (fw, fh):
        chain += f",crop={fw}:{fh}"          # kattalashtirilganda yon tomonlari kesiladi

    fade_out = max(0.0, length - 0.6)
    chain += (
        f",pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
        ",fade=t=in:st=0:d=0.4"
        f",fade=t=out:st={fade_out:.2f}:d=0.6"
        ",format=yuv420p[v]"
    )
    parts = [chain]

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
