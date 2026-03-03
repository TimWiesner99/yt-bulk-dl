#!/usr/bin/env python3
"""
Bulk YouTube Video Downloader
Opens a text file for pasting YouTube links, then downloads each video in the
highest available quality. Downloads are parallelised across multiple worker
threads so that several videos are fetched simultaneously.

Usage:
    python yt_bulk_download.py                          # basic download (4 workers)
    python yt_bulk_download.py -p DL_monday             # add prefix to filenames
    python yt_bulk_download.py -p DL_monday -l 50       # prefix + custom title length
    python yt_bulk_download.py -w 8                     # use 8 parallel download workers

Requirements:
    pip install yt-dlp
    ffmpeg must be installed and on PATH

Note: yt-dlp is the actively maintained successor to youtube-dl. It's faster,
has fewer issues with throttling, and supports more post-processing options.
"""

import sys
import os
import re
import csv
import json
import shutil
import subprocess
import argparse
import datetime
import platform
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("yt-dlp is not installed. Install it with:\n  pip install yt-dlp")
    sys.exit(1)


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DOWNLOAD_DIR = Path.home() / "Downloads" / "yt-bulk download"

DEFAULT_MAX_TITLE_LEN = 40
DEFAULT_WORKERS = 4


def check_ffmpeg():
    """Verify that ffmpeg is installed and on PATH. Exit with instructions if not."""
    if shutil.which("ffmpeg"):
        return
    print("Error: ffmpeg is not installed or not found on PATH.")
    print("ffmpeg is required to merge and re-encode video/audio streams.\n")
    print("Install it for your platform:")
    print("  Windows:              winget install ffmpeg")
    print("                    or  download from https://ffmpeg.org/download.html")
    print("  macOS:                brew install ffmpeg")
    print("  Linux (Debian/Ubuntu): sudo apt install ffmpeg")
    print("  Linux (Fedora):       sudo dnf install ffmpeg")
    sys.exit(1)

# Lock that serialises the check-then-rename step inside RenamePostProcessor
# to prevent TOCTOU races when two threads try to rename to the same filename.
_rename_lock = threading.Lock()


def sanitize_title(title: str, max_len: int) -> str:
    """Replace spaces with underscores, strip non-alphanumeric chars, and truncate."""
    # Replace spaces (and consecutive whitespace) with a single underscore
    clean = re.sub(r"\s+", "_", title.strip())
    # Remove anything that isn't alphanumeric, underscore, or hyphen
    clean = re.sub(r"[^\w\-]", "", clean)
    # Truncate — but avoid cutting in the middle of a word-boundary underscore
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip("_")
    return clean


def build_outtmpl(download_dir: Path) -> str:
    """
    Build the yt-dlp output template string.

    yt-dlp supports Python-style format expressions inside %(...)s. We use
    a custom 'before_dl' hook instead, because the built-in template language
    can't do regex replacements or truncation the way we want.

    So during download we use a temp name (the video ID), then rename after.
    """
    # Temporary name while downloading — the ID is always unique and filesystem-safe
    return str(download_dir / "%(id)s.%(ext)s")


def rename_file(filepath: str, prefix: str | None, max_len: int) -> str:
    """Rename a downloaded file according to our naming rules."""
    path = Path(filepath)
    if not path.exists():
        return filepath

    # yt-dlp may pass subtitle files too — handle any extension
    # For multi-dotted extensions like .en.srt, keep everything after the first dot
    stem = path.name.split(".")[0]       # this is the video ID (from our temp template)
    ext_parts = path.name.split(".")[1:]  # e.g. ['mp4'] or ['en', 'srt']
    extension = ".".join(ext_parts)

    return str(path.parent / f"{stem}.{extension}")


class EnsureH264PostProcessor(yt_dlp.postprocessor.PostProcessor):
    """
    Re-encode video to H264/AAC if the downloaded streams use different codecs.

    YouTube often serves AV1 or VP9 video and Opus audio.  These formats are
    not universally supported by all devices and editors, so this PP ensures
    the final file is always H264 video + AAC audio inside an MP4 container.
    If the file is already H264+AAC it is left untouched (no quality loss).
    """

    def run(self, info: dict):
        filepath = info.get("filepath", "")
        if not filepath or not Path(filepath).exists():
            return [], info

        video_codec, audio_codec = self._probe_codecs(filepath)
        needs_video = video_codec != "h264"
        needs_audio = audio_codec != "aac"

        if not needs_video and not needs_audio:
            return [], info

        if needs_video and needs_audio:
            msg = f"Re-encoding: video {video_codec}→h264, audio {audio_codec}→aac"
        elif needs_video:
            msg = f"Re-encoding video: {video_codec}→h264"
        else:
            msg = f"Re-encoding audio: {audio_codec}→aac"
        self.to_screen(msg)

        old_path = Path(filepath)
        temp_path = old_path.with_suffix(".tmp.mp4")

        cmd = ["ffmpeg", "-i", str(old_path), "-y"]
        if needs_video:
            cmd.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18"])
        else:
            cmd.extend(["-c:v", "copy"])
        if needs_audio:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            cmd.extend(["-c:a", "copy"])
        cmd.extend(["-movflags", "+faststart", str(temp_path)])

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            old_path.unlink()
            temp_path.rename(old_path)
        except subprocess.CalledProcessError as e:
            self.report_warning(
                f"ffmpeg re-encode failed: {e.stderr.decode(errors='replace')[:500]}"
            )
            if temp_path.exists():
                temp_path.unlink()

        return [], info

    @staticmethod
    def _probe_codecs(filepath: str) -> tuple[str, str]:
        """Use ffprobe to detect video and audio codec names."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_streams", filepath,
                ],
                capture_output=True, text=True, check=True,
            )
            streams = json.loads(result.stdout).get("streams", [])
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return ("unknown", "unknown")

        video_codec = "unknown"
        audio_codec = "unknown"
        for s in streams:
            if s.get("codec_type") == "video" and video_codec == "unknown":
                video_codec = s.get("codec_name", "unknown")
            elif s.get("codec_type") == "audio" and audio_codec == "unknown":
                audio_codec = s.get("codec_name", "unknown")
        return (video_codec, audio_codec)


class RenamePostProcessor(yt_dlp.postprocessor.PostProcessor):
    """
    A custom yt-dlp PostProcessor that renames files after download+merge.

    PostProcessors are yt-dlp's plugin mechanism for modifying files after
    they've been downloaded. Each PP receives an 'info' dict with metadata
    and the filepath, and returns the (possibly modified) info dict.
    """

    def __init__(self, prefix: str | None, max_len: int):
        super().__init__()
        self.prefix = prefix
        self.max_len = max_len

    def run(self, info: dict):
        title = info.get("title", info.get("id", "unknown"))
        video_id = info.get("id", "unknown")
        clean_title = sanitize_title(title, self.max_len)

        # Build the new base name
        if self.prefix:
            new_base = f"{self.prefix}_{clean_title}"
        else:
            new_base = clean_title

        # Rename the main video file
        old_path = Path(info["filepath"])
        ext = old_path.suffix                          # e.g. '.mp4'
        new_path = old_path.parent / f"{new_base}{ext}"
        if old_path.exists() and old_path != new_path:
            with _rename_lock:                         # serialise check+rename across threads
                new_path = _unique_path(new_path)
                old_path.rename(new_path)
            info["filepath"] = str(new_path)
            self.to_screen(f"Renamed → {new_path.name}")

        # Rename any sidecar subtitle files (e.g. <id>.en.srt)
        for srt in old_path.parent.glob(f"{video_id}.*.*"):
            lang_ext = srt.name.removeprefix(f"{video_id}")  # e.g. '.en.srt'
            new_srt = srt.parent / f"{new_base}{lang_ext}"
            if srt.exists() and srt != new_srt:
                with _rename_lock:
                    new_srt = _unique_path(new_srt)
                    srt.rename(new_srt)

        return [], info  # (list of files to delete, updated info dict)


def _unique_path(path: Path) -> Path:
    """If path already exists, append _1, _2, … to avoid overwriting."""
    if not path.exists():
        return path
    stem = path.stem
    suffixes = "".join(path.suffixes)
    counter = 1
    while path.exists():
        path = path.parent / f"{stem}_{counter}{suffixes}"
        counter += 1
    return path


class MetadataCollector(yt_dlp.postprocessor.PostProcessor):
    """Collect video metadata for CSV export.  Runs last so it captures the
    final (renamed) filepath."""

    def __init__(self):
        super().__init__()
        self.last_entry: dict | None = None

    def run(self, info: dict):
        upload_date = info.get("upload_date", "")
        if len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

        self.last_entry = {
            "filename": Path(info.get("filepath", "")).name,
            "title": info.get("title", ""),
            "channel": info.get("channel", info.get("uploader", "")),
            "upload_date": upload_date,
            "url": info.get("original_url", info.get("webpage_url", "")),
        }
        return [], info


def write_metadata_csv(rows: list[dict], output_path: Path):
    """Write video metadata to a CSV file preserving input link order."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "youtube_title", "channel", "upload_date", "youtube_url"])
        for row in rows:
            writer.writerow([
                row.get("filename", ""),
                row.get("title", ""),
                row.get("channel", ""),
                row.get("upload_date", ""),
                row.get("url", ""),
            ])


def build_opts(prefix: str | None, max_len: int, download_dir: Path) -> dict:
    """Construct the full yt-dlp options dict."""
    return {
        # Prefer H264 (avc1) video + AAC audio; fall back to best available
        # (the EnsureH264PostProcessor will re-encode non-H264 streams)
        "format": (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo+bestaudio/"
            "best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": build_outtmpl(download_dir),
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "noplaylist": True,
        "writesubtitles": True,
        "writeautomaticsub": False,
        "subtitleslangs": ["all"],
        "subtitlesformat": "srt",
        "writethumbnail": False,
    }


def _open_in_editor(filepath: str):
    """Open a file in the system's default text editor (best-effort, cross-platform)."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(filepath)
        elif system == "Darwin":
            subprocess.Popen(["open", filepath])
        else:
            editor = os.environ.get("EDITOR")
            if editor:
                subprocess.Popen([editor, filepath])
            else:
                subprocess.Popen(["xdg-open", filepath])
    except OSError:
        print(f"Could not open editor automatically. Please open this file manually:\n  {filepath}")


def prompt_for_links() -> list[str]:
    """Create a temp file, open it in an editor, and read back YouTube URLs."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="yt-bulk-dl_",
        delete=False,
        encoding="utf-8",
    )
    tmp.write("# Paste your YouTube URLs below, one per line.\n")
    tmp.write("# Lines starting with # are ignored.\n")
    tmp.write("# Save this file, then press Enter in the console to start downloading.\n\n")
    tmp.close()

    _open_in_editor(tmp.name)

    print(f"A text file has been opened for editing:\n  {tmp.name}\n")
    print("Paste your YouTube URLs (one per line), save the file,")
    input("then press Enter here to start downloading...")
    print()

    try:
        text = Path(tmp.name).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    links = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            links.append(stripped)
    return links


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk-download YouTube videos from a list file."
    )
    parser.add_argument(
        "-p", "--prefix",
        type=str,
        default=None,
        help="Optional prefix prepended to every filename (e.g. 'DL_monday').",
    )
    parser.add_argument(
        "-l", "--max-length",
        type=int,
        default=DEFAULT_MAX_TITLE_LEN,
        help=f"Max character length for the title portion of the filename "
             f"(default: {DEFAULT_MAX_TITLE_LEN}).",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of videos to download in parallel (default: {DEFAULT_WORKERS}).",
    )
    return parser.parse_args()


def download_one(link: str, opts: dict, prefix: str | None, max_len: int) -> dict:
    """Download a single video using a dedicated YoutubeDL instance.

    Each call creates its own YoutubeDL object so that multiple threads can
    run downloads concurrently without sharing state.  Returns a metadata dict
    suitable for appending to the CSV rows list.
    """
    collector = MetadataCollector()
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(EnsureH264PostProcessor(), when="post_process")
        ydl.add_post_processor(RenamePostProcessor(prefix, max_len), when="post_process")
        ydl.add_post_processor(collector, when="post_process")
        ydl.download([link])
    return collector.last_entry or {
        "filename": "", "title": "", "channel": "", "upload_date": "", "url": link,
    }


def main():
    args = parse_args()

    check_ffmpeg()

    links = prompt_for_links()
    if not links:
        print("No links found — nothing to do.")
        return

    batch_ts = datetime.datetime.now().strftime("%y-%m-%d %H-%M")
    download_dir = BASE_DOWNLOAD_DIR / batch_ts
    download_dir.mkdir(parents=True, exist_ok=True)

    opts = build_opts(args.prefix, args.max_length, download_dir)

    print(f"Found {len(links)} link(s). Downloading to: {download_dir}")
    if args.prefix:
        print(f"  Prefix:          {args.prefix}")
    print(f"  Max title length: {args.max_length}")
    print(f"  Workers:          {args.workers}")
    print(f"  Format:           H264 video + AAC audio (MP4)\n")

    # executor.map preserves input order, so the CSV rows stay in sync with
    # download-list.txt even though downloads run concurrently.
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        metadata_rows = list(executor.map(
            lambda link: download_one(link, opts, args.prefix, args.max_length),
            links,
        ))

    csv_path = download_dir / "metadata.csv"
    write_metadata_csv(metadata_rows, csv_path)
    print(f"\nMetadata written to: {csv_path}")

    print("\nAll done!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nDownload cancelled by user.")
    except Exception as e:
        print(f"\nUnexpected error: {e}")
    finally:
        input("\nPress Enter to exit...")
