"""
Kinodan reels uchun eng qiziqarli joylarni topish.

Tez ishlaydi: videoni to'liq dekodlamaydi. Uchta belgiga qaraydi:
  1) ovoz balandligi va keskin o'zgarishlari (portlash, qichqiriq, musiqa avji)
  2) video bitreyti — harakat ko'p bo'lgan sahnalarda kodek ko'proq bayt sarflaydi
  3) kalit kadrlar zichligi — tez-tez sahna almashinuvi (montaj tezligi)

Har bir soniyaga ball qo'yiladi, keyin bir-birini qoplamaydigan
eng yuqori ballli oraliqlar tanlanadi.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

import numpy as np

AUDIO_SR = 8000  # tahlil uchun yetarli, tez ishlaydi


@dataclass
class MediaInfo:
    path: str
    duration: float
    start_offset: float
    width: int            # burilish hisobga olingan, kesishdan oldingi o'lcham
    height: int
    sar: float            # piksel nisbati (anamorf DVD lar uchun)
    video_index: int      # ffmpeg dagi absolyut oqim raqami
    audio_index: int | None
    crop: tuple[int, int, int, int] | None = None  # w, h, x, y (qora chiziqlarsiz)

    @property
    def display_size(self) -> tuple[int, int]:
        """Kesish va SAR dan keyingi haqiqiy ko'rinish o'lchami."""
        w, h = (self.crop[0], self.crop[1]) if self.crop else (self.width, self.height)
        return max(2, round(w * self.sar)), h


@dataclass
class Clip:
    start: float
    end: float
    score: float
    rank: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Analysis:
    info: MediaInfo
    clips: list[Clip] = field(default_factory=list)


# --------------------------------------------------------------------------- probe

def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _fraction(value: str | None, default: float = 1.0) -> float:
    if not value or value in ("0:1", "0/1", "N/A"):
        return default
    sep = ":" if ":" in value else "/"
    try:
        a, b = value.split(sep)
        return float(a) / float(b) if float(b) else default
    except ValueError:
        return default


def _rotation(stream: dict) -> int:
    rot = stream.get("tags", {}).get("rotate")
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    try:
        return int(float(rot or 0)) % 360
    except ValueError:
        return 0


def probe(path: str) -> MediaInfo:
    data = json.loads(_run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path,
    ]))
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ValueError("Faylda video oqimi topilmadi")
    v = videos[0]

    duration = float(fmt.get("duration") or v.get("duration") or 0)
    try:
        vdur = float(v.get("duration") or 0)
        if 0 < vdur < duration:
            duration = vdur  # audio uzunroq bo'lsa ham, video bo'yicha hisoblaymiz
    except ValueError:
        pass
    if duration <= 0:
        raise ValueError("Video davomiyligini aniqlab bo'lmadi")

    w, h = int(v["width"]), int(v["height"])
    if _rotation(v) in (90, 270):
        w, h = h, w

    audios = [s for s in streams if s.get("codec_type") == "audio"]
    audio_index = None
    if audios:
        audio_index = audios[0]["index"]
        # O'zbekcha audio yo'lak bo'lsa — o'shani olamiz
        for s in audios:
            if (s.get("tags", {}).get("language") or "").lower() in ("uzb", "uz"):
                audio_index = s["index"]
                break
        else:
            for s in audios:
                if s.get("disposition", {}).get("default"):
                    audio_index = s["index"]
                    break

    return MediaInfo(
        path=path,
        duration=duration,
        start_offset=float(fmt.get("start_time") or 0),
        width=w,
        height=h,
        sar=_fraction(v.get("sample_aspect_ratio")),
        video_index=v["index"],
        audio_index=audio_index,
    )


def detect_crop(info: MediaInfo) -> tuple[int, int, int, int] | None:
    """Kino ichidagi qora chiziqlarni (letterbox) aniqlaydi."""
    found = []
    for frac in (0.15, 0.35, 0.5, 0.65, 0.85):
        t = info.duration * frac
        r = subprocess.run([
            "ffmpeg", "-hide_banner", "-ss", f"{t:.2f}", "-i", info.path,
            "-map", f"0:{info.video_index}", "-t", "3",
            "-vf", "cropdetect=limit=24:round=2:reset=0", "-an", "-sn", "-f", "null", "-",
        ], capture_output=True, text=True)
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if m:
            found.append(tuple(int(x) for x in m[-1]))
    if not found:
        return None
    # Eng katta maydonni olamiz — qorong'i sahna tufayli ortiqcha kesib yubormaslik uchun
    cw, ch, cx, cy = max(found, key=lambda c: c[0] * c[1])
    if cw * ch >= 0.96 * info.width * info.height:
        return None
    if cw < info.width * 0.5 or ch < info.height * 0.4:
        return None
    return cw, ch, cx, cy


# ----------------------------------------------------------------------- signals

def audio_loudness_db(info: MediaInfo, n_sec: int) -> np.ndarray:
    """Har soniya uchun ovoz balandligi (dBFS)."""
    out = np.full(n_sec, -90.0, dtype=np.float64)
    if info.audio_index is None:
        return out
    proc = subprocess.Popen([
        "ffmpeg", "-v", "error", "-i", info.path, "-map", f"0:{info.audio_index}",
        "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "s16le", "-",
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sec_bytes = AUDIO_SR * 2
    buf = b""
    i = 0
    assert proc.stdout is not None
    while True:
        data = proc.stdout.read(sec_bytes * 60)
        if not data:
            break
        buf += data
        full = len(buf) // sec_bytes * sec_bytes
        if not full:
            continue
        arr = np.frombuffer(buf[:full], dtype=np.int16).astype(np.float32)
        arr = arr.reshape(-1, AUDIO_SR) / 32768.0
        rms = np.sqrt(np.mean(arr * arr, axis=1))
        db = 20 * np.log10(rms + 1e-5)
        take = min(len(db), n_sec - i)
        if take > 0:
            out[i:i + take] = db[:take]
        i += len(db)
        buf = buf[full:]
    proc.wait()
    return out


def video_activity(info: MediaInfo, n_sec: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Dekodlamasdan: har soniyadagi baytlar, kalit kadrlar soni va kalit kadr vaqtlari."""
    bytes_ps = np.zeros(n_sec, dtype=np.float64)
    keys_ps = np.zeros(n_sec, dtype=np.float64)
    key_times: list[float] = []
    proc = subprocess.Popen([
        "ffprobe", "-v", "error", "-select_streams", str(info.video_index),
        "-show_entries", "packet=pts_time,dts_time,size,flags", "-of", "csv=p=0", info.path,
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        parts = line.strip().split(",")
        if len(parts) < 4:
            continue
        ts = parts[0] if parts[0] not in ("", "N/A") else parts[1]
        try:
            t = float(ts) - info.start_offset
            size = int(parts[2])
        except ValueError:
            continue
        idx = int(t)
        if 0 <= idx < n_sec:
            bytes_ps[idx] += size
            if "K" in parts[3]:
                keys_ps[idx] += 1
        if "K" in parts[3] and t >= 0:
            key_times.append(t)
    proc.wait()
    return bytes_ps, keys_ps, np.array(sorted(key_times))


# ------------------------------------------------------------------------ scoring

def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(x) < k:
        return x
    return np.convolve(x, np.ones(k) / k, mode="same")


def _norm(x: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(x, 10), np.percentile(x, 98)
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def score_seconds(db: np.ndarray, bytes_ps: np.ndarray, keys_ps: np.ndarray) -> np.ndarray:
    loud = _norm(_smooth(db, 3))
    dynamics = _norm(_smooth(np.abs(np.diff(_smooth(db, 2), prepend=db[0])), 5))
    motion = _norm(_smooth(bytes_ps, 3))
    cuts = _norm(_smooth(keys_ps, 10))
    return 0.40 * loud + 0.15 * dynamics + 0.30 * motion + 0.15 * cuts


def _snap(t: float, key_times: np.ndarray, before: float, after: float) -> float:
    """Vaqtni yaqin atrofdagi kalit kadrga (odatda sahna boshi) suradi."""
    if len(key_times) == 0:
        return t
    lo, hi = np.searchsorted(key_times, [t - before, t + after])
    cand = key_times[lo:hi]
    if len(cand) == 0:
        return t
    return float(cand[np.argmin(np.abs(cand - t))])


def pick_clips(score: np.ndarray, duration: float, key_times: np.ndarray,
               count: int, length: int) -> list[Clip]:
    n = len(score)
    length = int(max(5, min(length, n)))
    if n <= length:
        return [Clip(0.0, duration, float(score.mean()) if n else 0.0, 1)]

    # Boshidagi logotiplar va oxiridagi titrlarni o'tkazib yuboramiz
    if duration > 1800:
        head, tail = 0.02 * duration, 0.08 * duration
    else:
        head, tail = 0.01 * duration, 0.03 * duration
    first = int(head)
    last = int(duration - tail - length)
    if last <= first:
        first, last = 0, n - length

    csum = np.concatenate([[0.0], np.cumsum(score)])
    win = (csum[length:] - csum[:-length]) / length  # win[s] = s..s+length oraliq o'rtachasi
    order = np.argsort(-win)

    chosen: list[int] = []
    for gap in (30, 10, 0):
        for s in order:
            s = int(s)
            if s < first or s > last:
                continue
            if all(abs(s - c) >= length + gap for c in chosen):
                chosen.append(s)
                if len(chosen) >= count:
                    break
        if len(chosen) >= count:
            break

    clips = []
    for s in chosen:
        start = max(0.0, _snap(float(s), key_times, before=4, after=2))
        end_target = start + length
        end = _snap(end_target, key_times, before=4, after=3)
        if not (length * 0.75 <= end - start <= length + 5):
            end = end_target
        end = min(end, duration)
        clips.append(Clip(round(start, 3), round(end, 3), float(win[s])))

    clips.sort(key=lambda c: -c.score)
    for i, c in enumerate(clips, 1):
        c.rank = i
    return clips


def find_highlights(path: str, count: int = 10, length: int = 40,
                    progress=None) -> Analysis:
    def step(msg):
        if progress:
            progress(msg)

    step("Fayl tekshirilmoqda")
    info = probe(path)
    n_sec = int(np.ceil(info.duration))

    step("Qora chiziqlar aniqlanmoqda")
    info.crop = detect_crop(info)

    step("Ovoz tahlil qilinmoqda")
    db = audio_loudness_db(info, n_sec)

    step("Sahnalar tahlil qilinmoqda")
    bytes_ps, keys_ps, key_times = video_activity(info, n_sec)

    step("Eng qiziq joylar tanlanmoqda")
    score = score_seconds(db, bytes_ps, keys_ps)
    clips = pick_clips(score, info.duration, key_times, count, length)
    return Analysis(info=info, clips=clips)


def fmt_time(t: float) -> str:
    t = int(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


if __name__ == "__main__":
    import sys

    res = find_highlights(sys.argv[1], progress=print)
    print("Crop:", res.info.crop)
    for c in res.clips:
        print(f"{c.rank:2d}. {fmt_time(c.start)} – {fmt_time(c.end)}  ball={c.score:.3f}")
