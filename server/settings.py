from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOCAL_API_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"
QUICK_TUNNEL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ACCOUNT_USERNAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?")


def account_usernames(value: str | None) -> tuple[str, ...]:
    """Parse the two private account IDs without exposing them in source."""
    accounts = tuple(part.strip() for part in (value or "").split(","))
    if (
        len(accounts) != 2
        or len(set(accounts)) != 2
        or any(ACCOUNT_USERNAME.fullmatch(account) is None for account in accounts)
    ):
        raise ValueError(
            "ACCOUNT_USERNAMES must contain exactly two distinct 1-32 character "
            "lowercase IDs separated by a comma"
        )
    return accounts


def url_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("URL must have a hostname and no credentials")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Remote URLs must use HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}"


def api_origin(value: str) -> str:
    """Validate the only API destinations supported by this deployment."""
    parsed = urlsplit(value)
    origin = url_origin(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("API URL has an invalid port") from error
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("API URL must be an origin without a path, query, or fragment")

    hostname = parsed.hostname or ""
    if hostname in LOCAL_API_HOSTS:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Local API URLs must use HTTP or HTTPS")
        return origin

    label = hostname.removesuffix(QUICK_TUNNEL_SUFFIX)
    quick_tunnel = (
        parsed.scheme == "https"
        and hostname.endswith(QUICK_TUNNEL_SUFFIX)
        and "." not in label
        and QUICK_TUNNEL_LABEL.fullmatch(label) is not None
        and port in (None, 443)
    )
    if not quick_tunnel:
        raise ValueError("Remote API URLs must be an HTTPS *.trycloudflare.com Quick Tunnel origin")
    return origin


def _path(value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else PROJECT_DIR / candidate


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_cache_dir: Path
    accounts: tuple[str, ...] = ("user-alpha", "user-beta")
    site_origins: tuple[str, ...] = ()
    model: str = "Qwen3-ASR-1.7B"
    aligner: str = "Qwen3-ForcedAligner-0.6B"
    device: str = "cuda:0"
    compute_type: str = "bfloat16"
    attention: str = "sdpa"
    stability_guard_seconds: float = 0.6
    model_warmup: bool = False
    session_hours: int = 24
    max_pending_chunks: int = 2
    max_upload_bytes: int = 512_000
    import_part_bytes: int = 480 * 1024
    max_import_bytes: int = 1024 * 1024 * 1024
    max_import_seconds: int = 4 * 60 * 60
    max_recordings_bytes: int = 20 * 1024 * 1024 * 1024
    recording_free_reserve_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if account_usernames(",".join(self.accounts)) != self.accounts:
            raise ValueError("Settings.accounts must contain two normalized account IDs")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_DIR / "server" / ".env", override=False)
        accounts = account_usernames(os.getenv("ACCOUNT_USERNAMES"))
        origins = tuple(
            url_origin(value.strip())
            for value in os.getenv("SITE_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if value.strip()
        )
        return cls(
            data_dir=_path(os.getenv("DATA_DIR", ".data")),
            model_cache_dir=_path(os.getenv("MODEL_CACHE_DIR", ".models")),
            accounts=accounts,
            site_origins=origins,
            model=os.getenv("ASR_MODEL", os.getenv("WHISPER_MODEL", "Qwen3-ASR-1.7B")),
            aligner=os.getenv("ASR_ALIGNER", "Qwen3-ForcedAligner-0.6B"),
            device=os.getenv("ASR_DEVICE", os.getenv("WHISPER_DEVICE", "cuda:0")),
            compute_type=os.getenv("ASR_DTYPE", os.getenv("WHISPER_COMPUTE_TYPE", "bfloat16")),
            attention=os.getenv("ASR_ATTENTION", "sdpa"),
            stability_guard_seconds=max(0.2, min(float(os.getenv("STABILITY_GUARD_SECONDS", "0.6")), 1.0)),
            model_warmup=os.getenv("MODEL_WARMUP", "0").strip().lower() in {"1", "true", "yes", "on"},
            session_hours=max(1, min(int(os.getenv("SESSION_HOURS", "24")), 168)),
            max_pending_chunks=max(1, min(int(os.getenv("MAX_PENDING_CHUNKS", "2")), 8)),
            # The static protocol uses 480 KiB import parts, and a 15-second
            # PCM16 WAV is 480,044 bytes. Never accept a configuration that
            # silently breaks either contract.
            max_upload_bytes=max(480 * 1024, min(int(os.getenv("MAX_UPLOAD_BYTES", "512000")), 2_000_000)),
            max_import_bytes=max(
                1024 * 1024,
                min(int(os.getenv("MAX_IMPORT_BYTES", str(1024 * 1024 * 1024))), 1024 * 1024 * 1024),
            ),
            max_import_seconds=max(60, min(int(os.getenv("MAX_IMPORT_SECONDS", str(4 * 60 * 60))), 4 * 60 * 60)),
            max_recordings_bytes=max(
                512 * 1024 * 1024,
                min(int(os.getenv("MAX_RECORDINGS_BYTES", str(20 * 1024 * 1024 * 1024))), 100 * 1024 * 1024 * 1024),
            ),
            recording_free_reserve_bytes=max(
                256 * 1024 * 1024,
                min(int(os.getenv("RECORDING_FREE_RESERVE_BYTES", str(1024 * 1024 * 1024))), 20 * 1024 * 1024 * 1024),
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "classroom.sqlite3"

    @property
    def model_path(self) -> Path:
        candidate = Path(self.model).expanduser()
        return candidate if candidate.is_absolute() else self.model_cache_dir / candidate

    @property
    def aligner_path(self) -> Path:
        candidate = Path(self.aligner).expanduser()
        return candidate if candidate.is_absolute() else self.model_cache_dir / candidate
