"""Configuration: config.toml at the repo root, stdlib tomllib, validated.

The bridge carries no secrets (NFR-05); everything here is paths and knobs.
`OZU_CAPTURE_CONFIG` overrides the config path, `OZU_CAPTURE_DATA` the data
dir — both exist for the tests, which build throwaway worlds, and for the
NAS container, which mounts its own config and data (deploy/nas/).

Two sources share one config: the voice source ([nas]/[whisper], the Mac
LaunchAgent) and the youtube source ([youtube], the NAS container). Each can
be switched off, and the sections a disabled source needs are not required —
the container has no SMB share and no WHISPER, the Mac has no playlist duty.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    share_url: str
    processed_subdir: str
    inbox_override: str
    quiesce_seconds: int
    walled_prefixes: tuple[str, ...]
    whisper_cmd: tuple[str, ...]
    whisper_cwd: str
    whisper_extra_path: tuple[str, ...]
    whisper_timeout: int
    approved_remote: str
    branch: str
    topic: str
    agent_label: str
    start_interval: int
    voice_enabled: bool = True
    # [youtube] — the capture source for the Learning booster
    # (Ozu's Learn sheet (ozu `DESIGN.md`, invariant 11) §6.1). Off unless configured.
    youtube_enabled: bool = False
    playlist_url: str = ""
    poll_minutes: int = 30
    max_per_tick: int = 3
    languages: tuple[str, ...] = ("en", "es", "fr", "pt")
    max_minutes: int = 180
    whisper_fallback: bool = True
    handoff_dir: str = ""
    youtube_topic: str = "learning"
    ytdlp_cmd: tuple[str, ...] = ("yt-dlp",)
    ytdlp_extra_args: tuple[str, ...] = ()
    ytdlp_timeout: int = 600
    ytdlp_upgrade: bool = False
    data_dir: Path = field(default=REPO_ROOT / "data")

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def vault_clone(self) -> Path:
        return self.data_dir / "vault"

    @property
    def walled_dir(self) -> Path:
        return self.data_dir / "walled"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "ozu-capture.log"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".lock"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.walled_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


def load(path: Path | None = None) -> Config:
    cfg_path = path or Path(os.environ.get("OZU_CAPTURE_CONFIG",
                                           REPO_ROOT / "config.toml"))
    try:
        raw = tomllib.loads(cfg_path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {cfg_path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config unreadable: {cfg_path}: {e}")

    def sect(name: str, required: bool = True) -> dict:
        v = raw.get(name)
        if v is None and not required:
            return {}
        if not isinstance(v, dict):
            raise ConfigError(f"config missing [{name}] section")
        return v

    vault, bridge = sect("vault"), sect("bridge", required=False)
    voice_enabled = bool(bridge.get("voice_enabled", True))
    nas = sect("nas", required=voice_enabled)
    whisper = sect("whisper", required=voice_enabled)
    agent = sect("agent", required=False)
    yt = sect("youtube", required=False)

    cfg = Config(
        share_url=str(nas.get("share_url", "")),
        processed_subdir=str(nas.get("processed_subdir", "processed")),
        inbox_override=str(nas.get("inbox_override", "")),
        quiesce_seconds=int(bridge.get("poll_quiesce_seconds", 60)),
        walled_prefixes=tuple(str(p).lower() for p in
                              bridge.get("walled_prefixes", [])),
        whisper_cmd=tuple(str(c) for c in whisper.get("cmd", [])),
        whisper_cwd=str(whisper.get("cwd", "")),
        whisper_extra_path=tuple(str(p) for p in
                                 whisper.get("extra_path", [])),
        whisper_timeout=int(whisper.get("timeout_seconds", 7200)),
        approved_remote=str(vault.get("approved_remote", "")),
        branch=str(vault.get("branch", "main")),
        topic=str(vault.get("topic", "voice")),
        agent_label=str(agent.get("label", "dev.ozu.capture")),
        start_interval=int(agent.get("start_interval", 120)),
        voice_enabled=voice_enabled,
        youtube_enabled=bool(yt.get("enabled", False)),
        playlist_url=str(yt.get("playlist_url", "")),
        poll_minutes=int(yt.get("poll_minutes", 30)),
        max_per_tick=int(yt.get("max_per_tick", 3)),
        languages=tuple(str(l) for l in
                        yt.get("languages", ["en", "es", "fr", "pt"])),
        max_minutes=int(yt.get("max_minutes", 180)),
        whisper_fallback=bool(yt.get("whisper_fallback", True)),
        handoff_dir=str(yt.get("handoff_dir", "")),
        youtube_topic=str(yt.get("topic", "learning")),
        ytdlp_cmd=tuple(str(c) for c in yt.get("ytdlp_cmd", ["yt-dlp"])),
        ytdlp_extra_args=tuple(str(a) for a in yt.get("extra_args", [])),
        ytdlp_timeout=int(yt.get("timeout_seconds", 600)),
        ytdlp_upgrade=bool(yt.get("upgrade_daily", False)),
        data_dir=Path(os.environ.get("OZU_CAPTURE_DATA", REPO_ROOT / "data")),
    )

    if not cfg.approved_remote:
        raise ConfigError("[vault] approved_remote is required — the bridge "
                          "refuses to push anywhere unapproved (FR-33)")
    if not cfg.topic or "/" in cfg.topic:
        raise ConfigError("[vault] topic must be a bare directory name")
    if cfg.voice_enabled:
        if not cfg.whisper_cmd:
            raise ConfigError("[whisper] cmd must be a non-empty command list")
        if not cfg.inbox_override and not cfg.share_url.startswith("smb://"):
            raise ConfigError("[nas] share_url must be an smb:// URL")
        if cfg.quiesce_seconds < 10:
            raise ConfigError("[bridge] poll_quiesce_seconds < 10 defeats the "
                              "half-uploaded-file gate (FR-04)")
    if cfg.youtube_enabled:
        if not cfg.playlist_url:
            raise ConfigError("[youtube] playlist_url is required when enabled")
        if not cfg.ytdlp_cmd:
            raise ConfigError("[youtube] ytdlp_cmd must be a non-empty list")
        if not cfg.youtube_topic or "/" in cfg.youtube_topic:
            raise ConfigError("[youtube] topic must be a bare directory name")
        if cfg.whisper_fallback and not cfg.handoff_dir:
            raise ConfigError("[youtube] handoff_dir is required while "
                              "whisper_fallback is on (FR-05)")
        if cfg.poll_minutes < 1 or cfg.max_per_tick < 1:
            raise ConfigError("[youtube] poll_minutes and max_per_tick must "
                              "be at least 1")
    if not cfg.voice_enabled and not cfg.youtube_enabled:
        raise ConfigError("both sources are disabled; nothing to run")
    return cfg
