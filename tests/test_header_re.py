"""The Build page-header pattern routes a stamp, never a mention.

`builds.header_re()` is the text-layer key that sends ink on a returning page
to a Build. The stamp it looks for is drawn by `layout.Document.render` in the
top margin of every page of a Build proposal and of a Build result section,
so the text layer always gives it a line of its own:

    <prefix> Build <id> r<n>        <prefix> Build <id> result

Pinned here, under the default brand and under a made-up brand with an older
name a device might still hold:
  · every stamp on the rendered sample documents matches, in any case, and
    the pages that carry none (an edition's cover, contents, mail) match
    nothing, though they name Build ids in prose;
  · a sentence that names a Build routes nothing — across a line break, with
    the prefix glued to another word, or as a line that merely starts like a
    stamp;
  · the Build filename pattern keeps to one line in the same way.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from test_pipeline import env  # noqa: F401,E402 — shared fixture

BID = "B-260906-7f3a"
STAMPED = ("build-propose", "build-result")


@pytest.fixture(params=["default", "acme"])
def brand(request, env, monkeypatch):  # noqa: F811
    """(brand, prefixes) with the config built from them."""
    import ozu.config as config
    if request.param == "acme":
        monkeypatch.setenv("OZU_BRAND", "ACME")
        monkeypatch.setenv("OZU_DIGEST_PREFIXES", "acme ,old ")
        names = ("ACME", ("acme", "old"))
    else:
        monkeypatch.delenv("OZU_BRAND", raising=False)
        monkeypatch.delenv("OZU_DIGEST_PREFIXES", raising=False)
        names = ("Ozu", ("ozu",))
    config._config = None
    yield names
    config._config = None


def _pages(kind: str, out: Path) -> list[str]:
    import corpus
    import fitz
    from ozu import textlayer
    from ozu.digest_pdf import (build_edition_pdf, build_propose_pdf,
                                build_result_pdf)
    build = {"build-propose": build_propose_pdf, "build-result": build_result_pdf,
             "edition": build_edition_pdf}[kind]
    path = build(corpus.CASES[kind]["payload"], out / f"{kind}.pdf")
    with fitz.open(path) as doc:
        return [textlayer.extract(p) for p in doc]


def _ids(text: str) -> list[str]:
    from ozu import builds
    return [builds.canonical_id(i) for i in builds.header_re().findall(text)]


@pytest.mark.parametrize("kind", STAMPED)
def test_every_page_of_a_build_document_routes_in_any_case(brand, kind, tmp_path):
    name, _ = brand
    pages = _pages(kind, tmp_path)
    assert pages
    for n, text in enumerate(pages):
        stamps = [ln for ln in text.splitlines()
                  if ln.startswith(f"{name} Build {BID} ")]
        assert stamps, (kind, n, "every page carries the stamp")
        for variant in (text, text.upper(), text.lower(), text.swapcase()):
            assert set(_ids(variant)) == {BID}, (kind, n, variant[:80])


def test_an_edition_routes_only_its_result_pages(brand, tmp_path):
    name, _ = brand
    pages = _pages("edition", tmp_path)
    stamp = f"{name} Build {BID} result"
    stamped = [n for n, t in enumerate(pages) if stamp in t.splitlines()]
    assert stamped, "the result section stamps its pages"
    for n, text in enumerate(pages):
        want = [BID] if n in stamped else []
        assert _ids(text) == want, (n, text[:200])
        assert _ids(text.upper()) == want, n


def test_older_prefix_stamps_route(brand):
    _, prefixes = brand
    for p in prefixes:
        for word in (p, p.upper(), p.capitalize()):
            for tail in ("r2", "R12", "result", "RESULT"):
                text = f"printed body\n{word} Build {BID} {tail}\nmore body"
                assert _ids(text) == [BID], (word, tail)
                assert _ids(f"{word}  build\t{BID.upper()} {tail}  ") == [BID]


def _mentions(p: str) -> list[str]:
    return [
        # a mail line wrapped right before "Build"
        f"The kettle plan is parked. Drop the {p}\nBuild {BID} r1 if it is stale.",
        f"waiting on {p}\nBuild {BID} r1",
        # a line break between the words of an otherwise exact stamp
        f"{p}\nBuild {BID} r2",
        f"{p} Build\n{BID} r2",
        f"{p} Build {BID}\nr2",
        # the prefix glued to another word, before or after
        f"K{p} Build {BID} r2",
        f"x{p} Build {BID} result",
        f"9{p} Build {BID} r2",
        f"{p}ish Build {BID} r2",
        f"{p}Build {BID} r2",
        # a sentence that names a Build
        f"the {p} build {BID} finished",
        f"{p} Build {BID} r2 is waiting on your device",
        f"{p} Build {BID} · result · kettle and more",
        f"see {p} Build {BID}",
    ]


def test_a_mention_routes_nothing(brand):
    _, prefixes = brand
    for p in prefixes:
        for word in (p, p.upper(), p.capitalize()):
            for text in _mentions(word):
                assert _ids(text) == [], repr(text)


def test_match_build_ignores_a_wrapped_mention(brand):
    from ozu import builds
    _, prefixes = brand
    page = f"Your checkmarks\nwaiting on {prefixes[0]}\nBuild {BID} r1\n"
    assert builds.match_build("scan", page, None) == (None, "no key")


def test_build_filename_stays_on_one_line(brand):
    from ozu import edition
    name, prefixes = brand
    m = edition._build_name().match(f"{name} Build r2 - Kettle pricing ({BID})")
    assert m and m.group(2) == BID
    for p in prefixes:
        assert edition._build_name().match(f"{p}\nBuild r1 - x ({BID})") is None
        assert edition._build_name().match(f"{p} Build\nr1 - x ({BID})") is None
        assert edition._build_name().match(f"K{p} Build r1 - x ({BID})") is None
        assert edition._build_name().match(f"{p}Build r1 - x ({BID})") is None
