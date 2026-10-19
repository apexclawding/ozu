"""Write `talk.head.vtt`: two minutes of synthetic YouTube auto-captions.

Real auto-captions roll: every line appears first on its own with word
timings (`word<00:00:01.520><c> next</c>…`), then again for 10 ms as the
top line of the next cue, then once more above the line that follows it.
The parser has to drop those repeats and the timing tags, so the fixture
reproduces that shape exactly — with words written for the test suite
rather than someone else's captions.

    python3 tests/fixtures/make_vtt.py
"""
from pathlib import Path

TEXT = (
    "What you're looking at right over here is a page from my lab notebook. "
    "Every page has a date at the top, a question in the first line, and "
    "nothing torn out, ever. That last rule sounds fussy until the day you "
    "need to know what you tried in March and why it did not work. So today "
    "I want to show you how I keep one, and why I think the habit matters "
    "more than any tool you could buy for it. The first thing is the "
    "question. Before I touch anything I write down what I expect to happen "
    "and why. It takes thirty seconds, and it is the line I read first when "
    "I come back to the page a month later. The second thing is the result, "
    "written as it happens, with the time beside it, even when it is boring. "
    "Especially when it is boring, because the boring runs are the ones you "
    "forget you did. The third thing is the link. When a page builds on an "
    "older one, I write the older page number in the margin, and over a year "
    "those numbers turn the notebook into a map of how I actually think. "
    "That map is the whole point. A notebook you can follow backwards is "
    "worth ten notebooks you can only read forwards, and it costs nothing "
    "but the habit of writing one number down."
)
WORDS_PER_LINE = 7
SECONDS_PER_WORD = 0.32


def ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def main() -> None:
    words = TEXT.split()
    lines = [words[i:i + WORDS_PER_LINE] for i in range(0, len(words), WORDS_PER_LINE)]
    out = ["WEBVTT", "Kind: captions", "Language: en", ""]
    t, prev = 0.0, None
    for line in lines:
        start, end = t, t + len(line) * SECONDS_PER_WORD
        timed = line[0] + "".join(
            f"<{ts(start + i * SECONDS_PER_WORD)}><c> {w}</c>"
            for i, w in enumerate(line[1:], 1))
        out += [f"{ts(start)} --> {ts(end)} align:start position:0%",
                prev if prev else " ", timed, ""]
        prev = " ".join(line)
        out += [f"{ts(end)} --> {ts(end + 0.01)} align:start position:0%",
                prev, " ", ""]
        t = end + 0.01
    path = Path(__file__).parent / "talk.head.vtt"
    path.write_text("\n".join(out) + "\n")
    print(f"wrote {path} ({len(lines)} lines, {t:.0f}s)")


if __name__ == "__main__":
    main()
