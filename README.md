# YouTube Bulk Downloader

Single-file Python script that batch-downloads YouTube videos in the highest available quality. Reads URLs from a text file, merges the best video + audio streams, and optionally fetches manually-added subtitles as `.srt` sidecar files.

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

3. Run the script — downloads go to `./download/`.

## Usage

```bash
# Basic download
python yt_bulk_download.py

# Add a prefix to all filenames
python yt_bulk_download.py -p DL_monday

# Prefix + custom max title length (default: 30 characters)
python yt_bulk_download.py -p DL_monday -l 50
```

| Flag | Description |
|------|-------------|
| `-p`, `--prefix` | String prepended to every filename |
| `-l`, `--max-length` | Max characters for the title portion (default: 30) |

## Output example

```
download/
├── DL_monday_Rick_Astley_Never_Gonna_Gi.mp4
├── DL_monday_Rick_Astley_Never_Gonna_Gi.en.srt
└── DL_monday_Some_Other_Video_Title_Her.mp4
```

## Notes

- Auto-generated subtitles are excluded; only manually-added subs are downloaded.
- Playlist links download only the single linked video (set `noplaylist: False` in the script to change this).
- Partially downloaded files are automatically resumed on re-run.
