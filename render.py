"""
Tanlangan oraliqni vertikal (1080x1920) reels videoga aylantirish.

Ko'rinish: o'rtada kino kadri, tepasi va pasti qora fon. Matn yo'q.
Ovoz telefon uchun normallashtiriladi.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from analyze import Cancelled, Clip, MediaInfo

OUT_W, OUT_H = 1080, 1920


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


def build_filter(info: MediaInfo, length: float, has_overlay: bool = False,
                 zoom: float = 1.0) -> tuple[str, bool]:
    """Qora fon ustida markazda kino kadri; ixtiyoriy matn qatlami (1-kirish PNG) tepada."""
    sw, sh, fw, fh = _fg_size(info, zoom)

    chain = f"[0:{info.video_index}]"
    if info.crop:
        cw, ch, cx, cy = info.crop
        chain += f"crop={cw}:{ch}:{cx}:{cy},"
    chain += f"scale={sw}:{sh},setsar=1"
    if (sw, sh) != (fw, fh):
        chain += f",crop={fw}:{fh}"          # kattalashtirilganda yon tomonlari kesiladi

    fade_out = max(0.0, length - 0.6)
    chain += f",pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
    tail = (
        "fade=t=in:st=0:d=0.4"
        f",fade=t=out:st={fade_out:.2f}:d=0.6"
        ",format=yuv420p[v]"
    )
    if has_overlay:
        parts = [chain + "[base]", "[1:v]format=rgba[ov]", "[base][ov]overlay=0:0:format=auto," + tail]
    else:
        parts = [chain + "," + tail]

    has_audio = info.audio_index is not None
    if has_audio:
        parts.append(
            f"[0:{info.audio_index}]aformat=channel_layouts=stereo,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
            f"afade=t=in:st=0:d=0.3,afade=t=out:st={fade_out:.2f}:d=0.6[a]"
        )
    return ";".join(parts), has_audio


def top_bar_height(info: MediaInfo, zoom: float = 1.0) -> int:
    """Kadr ustidagi qora joy balandligi (matn shu yerga joylanadi)."""
    return (OUT_H - _fg_size(info, zoom)[3]) // 2


def render_clip(info: MediaInfo, clip: Clip, out_path: Path,
                overlay: Path | None = None, zoom: float = 1.0, timeout: int = 900,
                cancel=None) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    length = clip.duration
    fc, has_audio = build_filter(info, length, overlay is not None, zoom)

    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{clip.start:.3f}", "-i", info.path]
    if overlay is not None:
        cmd += ["-i", str(overlay)]
    cmd += ["-filter_complex", fc, "-t", f"{length:.3f}", "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    while True:
        try:
            _, err = proc.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                out_path.unlink(missing_ok=True)
                raise Cancelled()
            if time.monotonic() - t0 > timeout:
                proc.kill()
                proc.wait()
                raise RuntimeError("ffmpeg juda uzoq ishladi")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi: {(err or '').strip()[-500:]}")
    return out_path


def make_thumb(video: Path, at: float, out: Path) -> Path | None:
    r = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=180:320", "-q:v", "4", str(out),
    ], capture_output=True)
    return out if r.returncode == 0 and out.exists() else None
