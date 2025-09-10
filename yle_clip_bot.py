#!/usr/bin/env python3
import os, re, shlex, subprocess, pathlib, csv
from datetime import timedelta, datetime
from dataclasses import dataclass
from typing import List
import requests
from dotenv import load_dotenv
import dropbox
# === Config & Dropbox client ===
from dotenv import load_dotenv
load_dotenv(".env")  # load .env from project root explicitly

# Read target folder (defaults to /YleKlippen)
DROPBOX_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/YleKlippen")

def _get_dbx():
    """
    Prefer refresh-token auth (never expires). Falls back to short-lived token if set.
    Requires in .env:
      DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_OAUTH2_REFRESH_TOKEN
    Optional (fallback):
      DROPBOX_ACCESS_TOKEN
    """
    import dropbox
    rt  = os.getenv("DROPBOX_OAUTH2_REFRESH_TOKEN", "").strip()
    ak  = os.getenv("DROPBOX_APP_KEY", "").strip()
    ase = os.getenv("DROPBOX_APP_SECRET", "").strip()
    if rt and ak and ase:
        return dropbox.Dropbox(oauth2_refresh_token=rt, app_key=ak, app_secret=ase)
    tok = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()
    if tok:
        return dropbox.Dropbox(tok)
    return None

#!/usr/bin/env python3
import os, re, shlex, subprocess, pathlib, math, json, time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from datetime import datetime
import argparse

# 3rd party
from dotenv import load_dotenv
import requests
import numpy as np
import soundfile as sf
import librosa
import dropbox

# =============== ENV & GLOBALS ===============
ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DROPBOX_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/YleKlippen")

# ACRCloud (preferred)
ACR_HOST        = os.getenv("ACR_HOST", "").strip()
ACR_ACCESS_KEY  = os.getenv("ACR_ACCESS_KEY", "").strip()
ACR_ACCESS_SECRET = os.getenv("ACR_ACCESS_SECRET", "").strip()

# Audd.io (fallback)
AUDD_API_TOKEN  = os.getenv("AUDD_API_TOKEN", "").strip()

# =============== UTILITIES ===============
def run(cmd: str) -> subprocess.CompletedProcess:
    print(f"$ {cmd}")
    return subprocess.run(
        shlex.split(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

def hhmmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name

# =============== DROPBOX HELPER ===============
def _get_dbx():
    # Prefer refresh-token auth
    rt  = os.getenv("DROPBOX_OAUTH2_REFRESH_TOKEN", "").strip()
    ak  = os.getenv("DROPBOX_APP_KEY", "").strip()
    ase = os.getenv("DROPBOX_APP_SECRET", "").strip()
    if rt and ak and ase:
        return dropbox.Dropbox(oauth2_refresh_token=rt, app_key=ak, app_secret=ase)
    # Fallback: short-lived
    tok = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()
    if tok:
        return dropbox.Dropbox(tok)
    return None

def dropbox_ensure_folder(dbx: dropbox.Dropbox, folder: str):
    if not folder.startswith("/"): folder = "/" + folder
    cur = ""
    for part in [p for p in folder.split("/") if p]:
        cur += "/" + part
        try:
            dbx.files_get_metadata(cur)
        except dropbox.exceptions.ApiError:
            dbx.files_create_folder_v2(cur)

def upload_to_dropbox(local_path: str, target_folder: Optional[str] = None):
    dbx = _get_dbx()
    if not dbx:
        print("⚠️  No Dropbox credentials; skipping upload.")
        return
    folder = (target_folder or DROPBOX_FOLDER or "/YleKlippen")
    if not folder.startswith("/"): folder = "/" + folder
    dropbox_ensure_folder(dbx, folder)
    remote = f"{folder.rstrip('/')}/{os.path.basename(local_path)}"
    with open(local_path, "rb") as f:
        dbx.files_upload(f.read(), remote, mode=dropbox.files.WriteMode.overwrite)
    print(f"✅ Uploaded: {remote}")

# =============== DOWNLOAD ===============
def download_with_yle_dl(url: str, out_path: str) -> str:
    res = run(f"yle-dl -o {shlex.quote(out_path)} {shlex.quote(url)}")
    if res.returncode != 0:
        raise RuntimeError(f"yle-dl failed:\n{res.stderr}")
    print(f"✅ Download complete: {out_path}")
    return out_path

# =============== AUDIO & SEGMENTATION ===============
@dataclass
class Segment:
    start: float
    end: float
    label: str = ""    # filled later

def extract_audio(input_video: str, wav_path: str, sr: int = 22050) -> str:
    cmd = (
        f'ffmpeg -y -i {shlex.quote(input_video)} '
        f'-vn -ac 1 -ar {sr} -f wav {shlex.quote(wav_path)}'
    )
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extract failed:\n{res.stderr}")
    return wav_path

def detect_song_segments(wav_path: str, sr: int = 22050) -> List[Segment]:
    """
    Basic energy-based segmentation:
    - Compute RMS over 1s hops
    - Smooth, threshold to find high-energy regions
    - Merge close regions; keep only >= 60s
    """
    y, sr_read = sf.read(wav_path, dtype="float32")
    if y.ndim > 1:
        y = np.mean(y, axis=1)
    if sr_read != sr:
        # resample with librosa (ffmpeg already did, but safety)
        y = librosa.resample(y, orig_sr=sr_read, target_sr=sr)
    frame_length = int(sr * 1.0)
    hop_length = int(sr * 1.0)
    # frame RMS
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length, center=False)[0]
    times = np.arange(len(rms)) * (hop_length / sr)

    # smooth
    win = 5
    kernel = np.ones(win) / win
    rms_smooth = np.convolve(rms, kernel, mode="same")

    # threshold
    med = np.median(rms_smooth)
    thr = med * 2.5  # tweakable
    mask = rms_smooth > thr

    # find contiguous True regions
    segments: List[Segment] = []
    start_idx = None
    for i, m in enumerate(mask):
        if m and start_idx is None:
            start_idx = i
        elif not m and start_idx is not None:
            s = times[start_idx]
            e = times[i]
            if e - s >= 60:  # only keep >= 60s
                segments.append(Segment(start=s, end=e))
            start_idx = None
    if start_idx is not None:
        s = times[start_idx]
        e = times[-1] if len(times) else 0.0
        if e - s >= 60:
            segments.append(Segment(start=s, end=e))

    # merge segments closer than 8s (short gaps)
    merged: List[Segment] = []
    for seg in segments:
        if not merged:
            merged.append(seg); continue
        last = merged[-1]
        if seg.start - last.end <= 8:
            last.end = seg.end
        else:
            merged.append(seg)

    print(f"🔎 Detected {len(merged)} candidate song segments.")
    for i, s in enumerate(merged, 1):
        print(f"  {i:02d}: {hhmmss(s.start)} – {hhmmss(s.end)} ({int(s.end - s.start)}s)")
    return merged

# =============== RECOGNITION (ACRCloud / Audd) ===============
def recognize_with_acrcloud(wav_path: str, t_start: float, t_dur: float = 12.0) -> Optional[Tuple[str,str]]:
    """
    Slice 12s from wav at t_start and send to ACRCloud.
    Returns (artist, title) or None.
    """
    if not (ACR_HOST and ACR_ACCESS_KEY and ACR_ACCESS_SECRET):
        return None
    # temporary slice
    tmp = wav_path + f".acr_{int(t_start)}.wav"
    cmd = f'ffmpeg -y -ss {t_start:.3f} -t {t_dur:.3f} -i {shlex.quote(wav_path)} -ac 1 -ar 8000 -f wav {shlex.quote(tmp)}'
    if run(cmd).returncode != 0:
        return None

    # Build a simple ACRCloud request using their HTTP API
    # Docs: https://docs.acrcloud.com/reference/recognize
    import base64, hmac, hashlib
    requrl = f"http://{ACR_HOST}/v1/identify"
    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(int(time.time()))
    string_to_sign = f"{http_method}\n{http_uri}\n{ACR_ACCESS_KEY}\n{data_type}\n{signature_version}\n{timestamp}"
    sign = base64.b64encode(
        hmac.new(ACR_ACCESS_SECRET.encode(), string_to_sign.encode(), digestmod=hashlib.sha1).digest()
    ).decode()

    files = {
        'sample': open(tmp, 'rb'),
    }
    data = {
        'access_key': ACR_ACCESS_KEY,
        'data_type': data_type,
        'signature_version': signature_version,
        'signature': sign,
        'sample_bytes': os.path.getsize(tmp),
        'timestamp': timestamp,
    }
    try:
        r = requests.post(requrl, files=files, data=data, timeout=20)
        j = r.json()
        # Parse top hit
        if j.get("status", {}).get("code") == 0:
            md = j.get("metadata", {})
            musics = md.get("music", [])
            if musics:
                m0 = musics[0]
                title = m0.get("title", "")
                artists = ", ".join(a.get("name","") for a in m0.get("artists", []) if a.get("name"))
                if title or artists:
                    return (artists or "Unknown Artist", title or "Unknown Title")
    except Exception:
        pass
    finally:
        try: os.remove(tmp)
        except Exception: pass
    return None

def recognize_with_audd(wav_path: str, t_start: float, t_dur: float = 12.0) -> Optional[Tuple[str,str]]:
    """
    Slice 12s and send to Audd.io.
    Returns (artist, title) or None.
    """
    if not AUDD_API_TOKEN:
        return None
    tmp = wav_path + f".audd_{int(t_start)}.wav"
    cmd = f'ffmpeg -y -ss {t_start:.3f} -t {t_dur:.3f} -i {shlex.quote(wav_path)} -ac 2 -ar 44100 -f wav {shlex.quote(tmp)}'
    if run(cmd).returncode != 0:
        return None
    try:
        with open(tmp, 'rb') as f:
            files = {'file': f}
            data = {'api_token': AUDD_API_TOKEN, 'return': 'apple_music,deezer,spotify'}
            r = requests.post("https://api.audd.io/", data=data, files=files, timeout=20)
            j = r.json()
            if j.get("status") == "success" and j.get("result"):
                res = j["result"]
                title = res.get("title", "")
                artist = res.get("artist", "")
                if title or artist:
                    return (artist or "Unknown Artist", title or "Unknown Title")
    except Exception:
        pass
    finally:
        try: os.remove(tmp)
        except Exception: pass
    return None

def identify_segment(wav_path: str, seg: Segment) -> str:
    """
    Sample 12s near the middle of the segment for recognition.
    """
    mid = seg.start + (seg.end - seg.start) * 0.5
    # Try ACRCloud first
    tag = recognize_with_acrcloud(wav_path, t_start=mid)
    if not tag:
        tag = recognize_with_audd(wav_path, t_start=mid)
    if tag:
        artist, title = tag
        return sanitize_filename(f"{artist} - {title}")
    # fallback name
    return f"Unknown ({hhmmss(seg.start)}-{hhmmss(seg.end)})"

# =============== CUTTING ===============
def cut_clip(input_video: str, start: float, end: float, out_path: str):
    dur = max(0.1, end - start)
    # -ss before -i for fast seek, then re-encode copy if safe; we’ll use stream copy
    cmd = (
        f'ffmpeg -y -ss {start:.3f} -i {shlex.quote(input_video)} '
        f'-t {dur:.3f} -c copy {shlex.quote(out_path)}'
    )
    res = run(cmd)
    if res.returncode != 0:
        # If copy fails (bad keyframe align), re-encode
        cmd2 = (
            f'ffmpeg -y -ss {start:.3f} -i {shlex.quote(input_video)} '
            f'-t {dur:.3f} -c:v libx264 -c:a aac -movflags +faststart {shlex.quote(out_path)}'
        )
        res2 = run(cmd2)
        if res2.returncode != 0:
            raise RuntimeError(f"ffmpeg cut failed:\n{res.stderr}\n--\n{res2.stderr}")

# =============== MAIN PIPELINE ===============
def process(url: str, out_video: str, workdir: pathlib.Path):
    workdir.mkdir(parents=True, exist_ok=True)
    # 1) Download
    video_path = str(workdir / out_video)
    download_with_yle_dl(url, video_path)

    # 2) Extract audio wav
    wav_path = str(workdir / (pathlib.Path(out_video).stem + ".wav"))
    extract_audio(video_path, wav_path, sr=22050)

    # 3) Detect song-like segments
    segments = detect_song_segments(wav_path, sr=22050)
    if not segments:
        print("⚠️  No strong song-like segments found; uploading full video instead.")
        upload_to_dropbox(video_path, DROPBOX_FOLDER)
        return

    # 4) Identify & cut each segment
    clips_out = []
    clips_dir = workdir / (pathlib.Path(out_video).stem + "_clips")
    clips_dir.mkdir(exist_ok=True)

    for i, seg in enumerate(segments, 1):
        label = identify_segment(wav_path, seg)
        label = sanitize_filename(label) or f"Segment_{i}"
        clip_name = f"{i:02d} - {label} [{hhmmss(seg.start)}-{hhmmss(seg.end)}].mp4"
        clip_path = str(clips_dir / clip_name)
        print(f"✂️  Cutting: {clip_name}")
        cut_clip(video_path, seg.start, seg.end, clip_path)
        clips_out.append(clip_path)

    # 5) Upload all clips
    folder = DROPBOX_FOLDER.rstrip("/") + "/" + sanitize_filename(pathlib.Path(out_video).stem)
    for p in clips_out:
        upload_to_dropbox(p, folder)

    print("✅ All done.")

# =============== CLI ===============
def main():
    parser = argparse.ArgumentParser(description="Download Areena video, detect songs, cut clips, upload to Dropbox")
    parser.add_argument("url", help="Areena video URL")
    parser.add_argument("--out", default="areena.mp4", help="Output video filename")
    parser.add_argument("--workdir", default=".", help="Working directory")
    args = parser.parse_args()

    workdir = pathlib.Path(args.workdir).resolve()
    process(args.url, args.out, workdir)

if __name__ == "__main__":
    main()
