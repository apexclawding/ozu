"""Stand-in for yt-dlp in the youtube-source tests.

Answers by argv exactly the way run.py/youtube.py call the real thing, and
by video id the way the tests need it:

    vid-cap     14m35s, en-US, auto captions only (en-orig, en, es)
    vid-manual  Spanish video with a manual `es` track and an auto `en`
    vid-long    4h01m, would be skipped by the duration cap
    vid-nocap   no captions of any kind → hand-off / skipped
    vid-fail    per-video metadata exits 1 with YouTube's bot wall text

A playlist URL containing "broken" fails to list. Every invocation is
appended, as a JSON line, to $FAKE_YTDLP_LOG when set, so tests can count
calls (the poll gate, the listing-failure path). The VTT written for a
caption fetch is the synthetic fixture (tests/fixtures/make_vtt.py).
"""
import json
import os
import sys
from pathlib import Path

FIXTURE_VTT = Path(__file__).parent / "fixtures" / "talk.head.vtt"

VIDEOS = {
    "vid-cap": {
        "id": "vid-cap", "title": "Fake Talk: Captions [pilot]",
        "channel": "Fake Channel", "uploader": "Fake Channel",
        "duration": 875, "upload_date": "20260703", "language": "en-US",
        "webpage_url": "https://www.youtube.com/watch?v=vid-cap",
        "subtitles": {},
        "automatic_captions": {"en-orig": [{"ext": "vtt"}], "en": [{"ext": "vtt"}],
                               "es": [{"ext": "vtt"}]},
    },
    "vid-manual": {
        "id": "vid-manual", "title": "Charla con subtítulos",
        "channel": "Canal", "duration": 600, "upload_date": "20260101",
        "language": "es",
        "webpage_url": "https://www.youtube.com/watch?v=vid-manual",
        "subtitles": {"es": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt"}], "es-orig": [{"ext": "vtt"}]},
    },
    "vid-long": {
        "id": "vid-long", "title": "Four Hour Lecture", "channel": "Long U",
        "duration": 14460, "upload_date": "20250505", "language": "en",
        "webpage_url": "https://www.youtube.com/watch?v=vid-long",
        "subtitles": {}, "automatic_captions": {"en": [{"ext": "vtt"}]},
    },
    "vid-nocap": {
        "id": "vid-nocap", "title": "Silent Film Restored", "channel": "Archive",
        "duration": 300, "upload_date": "20240202", "language": "",
        "webpage_url": "https://www.youtube.com/watch?v=vid-nocap",
        "subtitles": {}, "automatic_captions": {},
    },
}


def _log(argv):
    path = os.environ.get("FAKE_YTDLP_LOG")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps(argv) + "\n")


def _video_id(url: str) -> str:
    return url.split("v=", 1)[1] if "v=" in url else url


def main() -> int:
    argv = sys.argv[1:]
    _log(argv)
    if argv == ["--version"]:
        print("2026.08.19-fake")
        return 0
    url = argv[-1]

    if "--flat-playlist" in argv:
        if "broken" in url:
            sys.stderr.write("ERROR: [youtube:tab] The playlist does not exist.\n")
            return 1
        ids = json.loads(os.environ.get("FAKE_YTDLP_ENTRIES", '["vid-cap"]'))
        entries = [{"id": i, "title": VIDEOS.get(i, {}).get("title", i),
                    "duration": VIDEOS.get(i, {}).get("duration", 0),
                    "url": f"https://www.youtube.com/watch?v={i}"} for i in ids]
        print(json.dumps({"title": "Learn", "entries": entries}))
        return 0

    vid = _video_id(url)
    if "-J" in argv:
        if vid == "vid-fail" or vid not in VIDEOS:
            sys.stderr.write("ERROR: [youtube] %s: Sign in to confirm you're "
                             "not a bot.\n" % vid)
            return 1
        print(json.dumps(VIDEOS[vid]))
        return 0

    if "-o" not in argv:
        sys.stderr.write("fake yt-dlp: unrecognised call\n")
        return 2
    template = argv[argv.index("-o") + 1]

    if "--skip-download" in argv:                     # caption fetch
        lang = argv[argv.index("--sub-langs") + 1]
        want_auto = "--write-auto-subs" in argv
        v = VIDEOS[vid]
        tracks = v["automatic_captions"] if want_auto else v["subtitles"]
        if lang not in tracks:
            return 0                                  # yt-dlp: exit 0, no file
        out = Path(template.replace("%(id)s", vid).replace("%(ext)s",
                                                           f"{lang}.vtt"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(FIXTURE_VTT.read_text())
        return 0

    if "-f" in argv:                                  # audio download
        out = Path(template.replace("%(id)s", vid).replace("%(ext)s", "m4a"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKE-AUDIO-" + vid.encode() * 200)
        return 0

    sys.stderr.write("fake yt-dlp: unrecognised call\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
