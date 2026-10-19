"""Stand-in for the WHISPER CLI in integration tests.

Behaves by filename, mirroring the real CLI's contract: writes a §7 file to
--out and exits 0, or writes nothing and exits non-zero ('boom')."""
import sys
from pathlib import Path

GOLDEN = '''---
title: "{title}"
source_file: "{name}"
source_sha256: "notusedbybridge"
duration: "00:07:41"
duration_seconds: 461
language: "es"
language_confidence: 0.97
translated: false
model: "mlx-community/whisper-large-v3-turbo"
engine: "mlx-whisper 0.4.3"
transcribed_at: "2026-08-29T10:00:00+02:00"
processing_seconds: 55
realtime_factor: 8.4
word_count: 812
tags: [transcript]
---

# {title}

> Transcribed locally with Whisper large-v3-turbo · 7m 41s · Spanish · 2026-08-29

## Transcript

**[00:00:00]** Primer párrafo del memo con ideas.

**[00:01:30]** Segundo párrafo con más contexto.
'''

NO_SPEECH = '''---
title: "{title}"
language: ""
duration_seconds: 30
---

# {title}

## Transcript

_No speech detected._
'''


def main() -> int:
    args = sys.argv[1:]
    audio = Path(args[0])
    out = Path(args[args.index("--out") + 1])
    if "boom" in audio.name:
        sys.stderr.write("ffmpeg: decode error: engine exploded\n")
        return 3
    tpl = NO_SPEECH if "silent" in audio.name else GOLDEN
    out.write_text(tpl.format(title=audio.stem, name=audio.name))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
