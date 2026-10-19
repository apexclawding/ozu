"""Which model reads what, and what the brief says it cost (D-10).

Measured on the live corpus 2026-09-20, six returning pages and five notebook
pages, same prompts, same effort:

  · ticks        Opus 27 found / 0 missed / 0 invented; Sonnet identical
  · @input       Opus 3 of 3; Sonnet 3 of 3
  · handwriting  transcription matched 92-100 %, confidence within 0.05

So the argument that put Opus on every page — handwriting OCR is the
accuracy bottleneck — does not hold for the *returning* pages, where the job
is narrow and the printed side is already in the prompt. It still holds for a
notebook page, where the difference that did show up lives: on one dense page
Opus wrote a single note and Sonnet wrote five atomic ones. Neither is wrong,
and that judgement shapes the vault permanently, so the original capture
stays on the stronger model.

Haiku 4.5 is not an option as the call stands: it rejects the reasoning
`effort` the schema call sends. That is a call-shape problem, not a quality
one, and this file records it so nobody re-discovers it by hand.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture


class FakeResp:
    def __init__(self):
        import json
        self.content = [type("B", (), {"type": "text",
                                       "text": json.dumps(MOCK_RESULT)})()]
        self.stop_reason = "end_turn"
        self.stop_details = type("D", (), {"category": None})()
        self.usage = type("U", (), {"input_tokens": 12000, "output_tokens": 150})()


@pytest.fixture()
def calls(monkeypatch):
    """Record the model each vision call asks for, without making one."""
    from ozu import understand
    seen = []

    def create(**kw):
        seen.append(kw["model"])
        return FakeResp()

    monkeypatch.setattr(understand, "_client",
                        lambda: type("C", (), {"messages": type("M", (), {
                            "create": staticmethod(create)})()})())
    return seen


def test_a_notebook_page_is_read_by_the_strong_model_and_a_return_by_the_cheap_one(
        env, calls):
    """The split, end to end through ingest: kind decides the model."""
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Inbox" / "ink.pdf", ["handwriting"])
    make_pdf(env.raw_dir / "Ozu Evening 2026-09-20  - Kindle.pdf", ["a document back"])
    ingest.ingest()
    understand.process_pending()

    assert sorted(calls) == sorted([env.model, env.annotation_model])
    assert env.model == "claude-opus-5"
    assert env.annotation_model == "claude-sonnet-5"


def test_either_model_can_be_overridden_without_touching_code(env, calls, monkeypatch):
    from ozu import ingest, understand
    monkeypatch.setattr(env, "annotation_model", "claude-opus-5")
    make_pdf(env.raw_dir / "Ozu Evening 2026-09-20  - Kindle.pdf", ["back"])
    ingest.ingest()
    understand.process_pending()
    assert calls == ["claude-opus-5"]


def test_the_journal_line_names_the_model_that_read_the_page(env, calls, caplog):
    """The journal line is the only record of what a page cost, and with two
    models in play it has to name the one that read the page."""
    import logging
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Ozu Evening 2026-09-20  - Kindle.pdf", ["back"])
    ingest.ingest()
    with caplog.at_level(logging.INFO, logger="ozu.understand"):
        understand.process_pending()
    read = [r.getMessage() for r in caplog.records if r.getMessage().startswith("read ")]
    assert read and "on claude-sonnet-5 at effort=high" in read[0], read


# --- what the brief says it cost --------------------------------------------



def test_each_run_kind_asks_for_its_own_model_and_the_defaults_are_deliberate(env):
    """These bill the claude.ai login, not the API key (invariant 7), so the
    choice buys usage headroom rather than money. Only `learn` is rightsized
    down by default; a proposal reads a repo and writes a plan that gets
    approved on paper, and research argues against the owner's own notes
    from arbitrary web pages — neither is where to save a usage window."""
    assert env.run_models == {"propose": "", "research": "",
                              "learn": "claude-sonnet-5", "wiki": ""}


def test_a_run_model_is_an_env_var_away(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_RESEARCH_MODEL", "claude-fable-5-1")
    monkeypatch.setenv("OZU_ANNOTATION_MODEL", "claude-opus-5")
    import ozu.config as config
    config._config = None
    cfg = config.get_config()
    assert cfg.run_models["research"] == "claude-fable-5-1"
    assert cfg.annotation_model == "claude-opus-5"
    config._config = None
