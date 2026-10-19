# Contributing

Ozu is a personal system that happens to be public. Fixes, tests and
clearer docs are welcome; large redesigns are better discussed in an issue
first. [`DESIGN.md`](DESIGN.md) lists the rules that tests do not always
catch, so read it before changing behaviour.

To report a security problem, see [`SECURITY.md`](SECURITY.md) instead of
opening an issue.

## Running the tests

Core (Python 3.12):

```bash
pip install -e ".[dev]"
pytest -q
ruff check --select F src
python tools/render_sample.py      # builds every document type, re-checks the gates
```

`capture/` uses only the standard library:

```bash
cd capture && PYTHONPATH=src python -m unittest discover -s . -p "test_*.py"
```

`transcribe/` runs on Apple Silicon only (CI uses a macOS runner):

```bash
cd transcribe && uv run python -m unittest discover -s tests
```

## Rules for a change

- Every behaviour change comes with a test. A bug fix comes with a test that
  fails without it.
- The suites are hermetic: no API keys, no network, the vision call and
  Whisper are faked. Keep it that way; a test that needs a key or a model
  will not be merged.
- Never commit secrets, real pages, or personal vault content. Fixtures are
  synthetic.

## Review and merge

Every pull request is read by the maintainer before it is merged, and CI must
be green: `core`, `capture` and `transcribe` (macOS). Code from outside contributors is reviewed before it
is run on anyone's machine; please keep changes small enough to read in one
sitting and explain why, not only what.

## License

By contributing you agree that your work is released under the MIT license in
[`LICENSE`](LICENSE).
