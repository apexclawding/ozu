"""CLI (FR-40..42): a thin client of core/pipeline.py, exactly like the web UI.

Errors go to stderr with a non-zero exit (FR-41). The model is never
downloaded implicitly — `prefetch-model` is the explicit consent path (R2).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from whisper_notes.config import ConfigError, load_config
from whisper_notes.core.audio import AudioError
from whisper_notes.core.engine import EngineError
from whisper_notes.core.engine_mlx import MlxWhisperEngine
from whisper_notes.core.pipeline import PipelineError, run_pipeline
from whisper_notes.logs import setup_logging
from whisper_notes.preflight import PreflightError, run_preflight


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whisper-notes",
        description="Local audio -> Markdown transcription. On-device only.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("transcribe", help="Transcribe an audio file to Markdown")
    t.add_argument("file", type=Path, help="Audio or video file to transcribe")
    t.add_argument("--model", help="Model repo or local path (overrides config)")
    t.add_argument("--language", help="Force a language code, e.g. 'pt' (skips auto-detect)")
    t.add_argument("--translate", action="store_true", help="Translate to English")
    t.add_argument("--no-timestamps", action="store_true", help="Omit paragraph timestamps")
    t.add_argument("--out", type=Path, help="Output .md path (default: ./data/transcripts/)")

    p = sub.add_parser("prefetch-model", help="Download the model weights once, explicitly")
    p.add_argument("--model", help="Model repo (overrides config)")

    s = sub.add_parser("serve", help="Start the local web UI on 127.0.0.1")
    s.add_argument("--port", type=int, help="Port (overrides config, default 8420)")
    return parser


def _progress_printer():
    state = {"phase": None, "last_pct": -10.0}

    def report(phase: str, percent: float | None) -> None:
        if phase != state["phase"]:
            state["phase"], state["last_pct"] = phase, -10.0
            if phase != "transcribing":
                print(f"-> {phase}", file=sys.stderr)
        if phase == "transcribing" and percent is not None:
            if percent - state["last_pct"] >= 10 or percent >= 100:
                state["last_pct"] = percent
                print(f"-> transcribing {percent:3.0f}%", file=sys.stderr)

    return report


def _cmd_transcribe(args: argparse.Namespace) -> int:
    run_preflight()
    config = load_config()
    setup_logging(config.data_dir)
    engine = MlxWhisperEngine(args.model or config.model_repo)
    if args.translate and not engine.supports_translation:
        from whisper_notes.core.engine_mlx import TRANSLATION_UNSUPPORTED_MESSAGE

        raise EngineError(TRANSLATION_UNSUPPORTED_MESSAGE.format(engine.model_repo))
    result = run_pipeline(
        args.file,
        engine=engine,
        config=config,
        language=args.language,
        translate=args.translate,
        timestamps=False if args.no_timestamps else None,
        out_path=args.out,
        progress=_progress_printer(),
    )
    if result.no_speech:
        print("note: no speech detected", file=sys.stderr)
    print(result.markdown_path)
    return 0


def _cmd_prefetch(args: argparse.Namespace) -> int:
    config = load_config()
    engine = MlxWhisperEngine(args.model or config.model_repo)
    if engine.is_cached():
        print(f"{engine.model_repo} is already cached.", file=sys.stderr)
        return 0
    print(
        f"Downloading {engine.model_repo} from Hugging Face (one-time, ~1.6 GB "
        "for the default model). All later inference is fully offline.",
        file=sys.stderr,
    )
    engine.prefetch()
    print("Model cached.", file=sys.stderr)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    run_preflight()  # FR-02: refuse to start without ffmpeg
    config = load_config()
    setup_logging(config.data_dir)
    port = args.port or config.port

    import uvicorn

    from whisper_notes.store.db import JobStore
    from whisper_notes.web.app import create_app
    from whisper_notes.web.queue import TranscriptionQueue

    store = JobStore(config.db_path)
    queue = TranscriptionQueue(store, config)
    app = create_app(config, store, queue)
    print(f"whisper-notes serving on http://127.0.0.1:{port}", file=sys.stderr)
    # 127.0.0.1 is deliberately hard-coded — never 0.0.0.0 (FR-30)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "transcribe":
            return _cmd_transcribe(args)
        if args.command == "serve":
            return _cmd_serve(args)
        return _cmd_prefetch(args)
    except (PreflightError, ConfigError, AudioError, EngineError, PipelineError) as e:
        print(f"whisper-notes: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("whisper-notes: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
