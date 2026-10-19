"""The brand is configuration (OZU_BRAND out, OZU_DIGEST_PREFIXES in).

Pinned here, with a made-up brand and an older name a device might still hold:
  · every outgoing name carries OZU_BRAND — edition and Build file names, the
    PDF Author, the Build page-header and result stamps, the PR body;
  · the Build id is still read back from the Build file name;
  · the incoming Build patterns are built from OZU_DIGEST_PREFIXES and match
    case-insensitively, so ink on a page printed under any configured name,
    in any case, still routes to its Build;
  · with nothing set, the defaults are the ones the rest of the suite sees.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from test_pipeline import env  # noqa: F401 — shared fixture

BID = "B-260906-7f3a"


@pytest.fixture
def acme(env, monkeypatch):  # noqa: F811
    import ozu.config as config
    monkeypatch.setenv("OZU_BRAND", "ACME")
    monkeypatch.setenv("OZU_DIGEST_PREFIXES", "acme ,old ")
    config._config = None
    yield env
    config._config = None


def test_defaults(env, monkeypatch):  # noqa: F811
    import ozu.config as config
    from ozu import builds, edition
    monkeypatch.delenv("OZU_BRAND", raising=False)
    monkeypatch.delenv("OZU_DIGEST_PREFIXES", raising=False)
    config._config = None
    assert config.brand() == "Ozu"
    assert edition.edition_name("morning", dt.date(2026, 9, 11)) == "Ozu Morning 2026-09-11"
    for text in (f"Ozu Build {BID} r2", f"OZU Build {BID} r2", f"ozu build {BID.lower()} r2"):
        assert [builds.canonical_id(i) for i in builds.header_re().findall(text)] == [BID]


def test_outgoing_names_carry_the_brand(acme):
    from ozu import builds, edition, layout
    from ozu.digest_pdf import result_header
    assert edition.edition_name("morning", dt.date(2026, 9, 11)) == "ACME Morning 2026-09-11"
    assert edition.edition_name("extra", dt.date(2026, 9, 11),
                                dt.datetime(2026, 9, 11, 16, 34)) == "ACME Extra 2026-09-11 1634"
    name = builds.pdf_name(BID, 2, "Kettle pricing")
    assert name == f"ACME Build r2 - Kettle pricing ({BID})"
    assert builds.ID_RE.search(name).group(0) == BID
    assert result_header(BID) == f"ACME Build {BID} result"
    assert dict(layout.AUTHORS) == {k: f"ACME {v}" for k, v in
                                    (("edition", "Edition"), ("build", "Build"),
                                     ("research", "Research"), ("digest", "Digest"),
                                     ("learn", "Learn"), ("mail", "Mail"))}


def test_header_re_matches_every_prefix_in_any_case(acme):
    from ozu import builds
    for prefix in ("ACME", "acme", "Acme", "old", "OLD", "Old"):
        for build in ("Build", "BUILD", "build"):
            ids = builds.header_re().findall(f"{prefix} {build} {BID} r2")
            assert [builds.canonical_id(i) for i in ids] == [BID], (prefix, build)
    upper = builds.header_re().findall(f"ACME BUILD {BID.upper()} result")
    assert [builds.canonical_id(i) for i in upper] == [BID]
    # an unconfigured name, and a bare mention, route nothing
    assert builds.header_re().findall(f"Other Build {BID} r2") == []
    assert builds.header_re().findall(f"see {BID}") == []


def test_match_build_reads_a_header_under_an_older_name(acme):
    from ozu import builds
    assert builds.match_build("scan", f"OLD BUILD {BID} r2", None)[0] == BID
    assert builds.match_build("scan", f"acme build {BID} r2", None)[0] == BID


def test_build_name_matches_both_prefixes(acme):
    from ozu import edition
    for stem in (f"ACME Build r2 - Kettle pricing ({BID})",
                 f"acme build r2 - Kettle pricing ({BID})",
                 f"old Build r1 - x ({BID})",
                 f"OLD BUILD r1 - x ({BID})"):
        m = edition._build_name().match(stem)
        assert m and m.group(2).lower() == BID.lower(), stem
    assert edition._build_name().match(f"Other Build r1 - x ({BID})") is None


def test_build_page_header_stamps_the_brand(acme, tmp_path):
    import fitz
    from ozu import textlayer
    from ozu.digest_pdf import build_propose_pdf
    out = build_propose_pdf({"id": BID, "rev": 2, "topic": "kettle",
                             "title": "Kettle pricing", "date": "2026-09-06",
                             "proposal": "A plan.", "history": ""},
                            tmp_path / "p.pdf")
    with fitz.open(out) as doc:
        assert doc.metadata["author"] == "ACME Build"
        assert all(f"ACME Build {BID} r2" in textlayer.extract(p) for p in doc)


def test_open_pr_body_carries_the_brand(acme, monkeypatch):
    import ozu.config as config
    from ozu import agent
    monkeypatch.setenv("OZU_GITHUB_TOKEN", "github_pat_test")
    config._config = None
    seen = {}

    class FakeResp:
        def read(self):
            return json.dumps({"html_url": "https://github.com/o/r/pull/7"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["payload"] = json.loads(req.data)
        return FakeResp()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    agent._open_pr("o/r", "main", "b", {"id": "n1", "topic": "kettle",
                                        "title": "t"}, "r")
    assert "Opened by ACME from ink note `n1`" in seen["payload"]["body"]
