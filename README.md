# YouTube Bulk Downloader

Single-file Python script that batch-downloads YouTube videos in the highest available quality. Reads URLs from a text file, merges the best video + audio streams, downloads multiple videos in parallel, and optionally fetches manually-added subtitles as `.srt` sidecar files.

## Requirements

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — `pip install yt-dlp`
- [ffmpeg](https://ffmpeg.org/) — needed to merge separate video/audio streams

## Setup

1. Place `yt_bulk_download.py` in your project folder.
2. Create a `download-list.txt` in the same folder with one YouTube URL per line:

```
https://www.youtube.com/watch?v=dQw4w9WgXcQ
https://www.youtube.com/watch?v=abc123
# Lines starting with # are ignored
```

3. Run the script — downloads go to `~/Downloads/yt-bulk download/<yy-mm-dd HH-MM>/`.

## Usage

```bash
# Basic download (4 parallel workers)
python yt_bulk_download.py

# Add a prefix to all filenames
python yt_bulk_download.py -p DL_monday

# Prefix + custom max title length (default: 40 characters)
python yt_bulk_download.py -p DL_monday -l 50

# Use 8 parallel download workers instead of the default 4
python yt_bulk_download.py -w 8

# All options combined
python yt_bulk_download.py -p DL_monday -l 50 -w 8
```

| Flag | Description |
|------|-------------|
| `-p`, `--prefix` | String prepended to every filename |
| `-l`, `--max-length` | Max characters for the title portion (default: 40) |
| `-w`, `--workers` | Number of videos downloaded in parallel (default: 4) |

## Output

Each run creates a timestamped subfolder inside `~/Downloads/yt-bulk download/`:

```
~/Downloads/yt-bulk download/
└── 26-02-26 14-30/
    ├── DL_monday_Rick_Astley_Never_Gonna_Give_You_Up.mp4
    ├── DL_monday_Rick_Astley_Never_Gonna_Give_You_Up.en.srt
    ├── DL_monday_Some_Other_Video_Title_Here_In_Full.mp4
    └── metadata.csv
```

`metadata.csv` contains one row per URL from `download-list.txt` (in input order), with columns: `filename`, `youtube_title`, `channel`, `upload_date`, `youtube_url`.

## Parallel downloads

By default, 4 videos are downloaded simultaneously. Each worker runs its own independent `yt-dlp` instance, so progress output from multiple downloads will be interleaved in the terminal. The order of rows in `metadata.csv` always matches the order of URLs in `download-list.txt` regardless of which download finishes first.

Increase `-w` for faster bulk runs (e.g. `-w 8`). Very high values (>8) are unlikely to help and may trigger YouTube rate-limiting.

## Notes

- Auto-generated subtitles are excluded; only manually-added subs are downloaded.
- Playlist links download only the single linked video (set `noplaylist: False` in the script to change this).
- Partially downloaded files are automatically resumed on re-run.
