from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOCAL_API_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"
QUICK_TUNNEL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ACCOUNT_USERNAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?")
MINDLOGIC_GATEWAY_HOST = "factchat-cloud.mindlogic.ai"
MINDLOGIC_GATEWAY_PATH = "/v1/gateway"


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


def mindlogic_gateway_base_url(value: str) -> str:
    """Keep the bearer credential pinned to the documented NOVA gateway."""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("MINDLOGIC_BASE_URL has an invalid port") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != MINDLOGIC_GATEWAY_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != MINDLOGIC_GATEWAY_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MINDLOGIC_BASE_URL must be the official HTTPS NOVA gateway")
    return f"https://{MINDLOGIC_GATEWAY_HOST}{MINDLOGIC_GATEWAY_PATH}"


def _path(value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else PROJECT_DIR / candidate


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_cache_dir: Path
    accounts: tuple[str, ...] = field(default=("user-alpha", "user-beta"), repr=False)
    # The administrator is a private deployment detail.  Excluding it from
    # repr prevents an otherwise convenient Settings log/debug statement from
    # publishing a real account ID.
    admin_username: str | None = field(default=None, repr=False)
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
    mindlogic_api_key: str | None = field(default=None, repr=False)
    mindlogic_base_url: str = f"https://{MINDLOGIC_GATEWAY_HOST}{MINDLOGIC_GATEWAY_PATH}"
    mindlogic_model: str = "solar-pro4"
    correction_chunk_chars: int = 6000
    correction_overlap_segments: int = 2
    correction_connect_timeout_seconds: float = 10.0
    correction_read_timeout_seconds: float = 90.0
    correction_max_retries: int = 2
    correction_retry_base_seconds: float = 1.0
    correction_max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if account_usernames(",".join(self.accounts)) != self.accounts:
            raise ValueError("Settings.accounts must contain two normalized account IDs")
        if self.admin_username is not None and (
            ACCOUNT_USERNAME.fullmatch(self.admin_username) is None
            or self.admin_username not in self.accounts
        ):
            # Do not reflect a possibly secret/mistyped account ID.
            raise ValueError("ADMIN_USERNAME must identify one configured account")
        normalized_gateway = mindlogic_gateway_base_url(self.mindlogic_base_url)
        object.__setattr__(self, "mindlogic_base_url", normalized_gateway)

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
            admin_username=(os.getenv("ADMIN_USERNAME") or "").strip() or None,
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
            mindlogic_api_key=(os.getenv("MINDLOGIC_API_KEY") or "").strip() or None,
            mindlogic_base_url=mindlogic_gateway_base_url(
                os.getenv(
                    "MINDLOGIC_BASE_URL",
                    f"https://{MINDLOGIC_GATEWAY_HOST}{MINDLOGIC_GATEWAY_PATH}",
                )
            ),
            mindlogic_model=(os.getenv("MINDLOGIC_MODEL", "solar-pro4").strip() or "solar-pro4"),
            correction_chunk_chars=max(1000, min(int(os.getenv("CORRECTION_CHUNK_CHARS", "6000")), 24000)),
            correction_overlap_segments=max(
                0, min(int(os.getenv("CORRECTION_OVERLAP_SEGMENTS", "2")), 5)
            ),
            correction_connect_timeout_seconds=max(
                1.0, min(float(os.getenv("CORRECTION_CONNECT_TIMEOUT_SECONDS", "10")), 30.0)
            ),
            correction_read_timeout_seconds=max(
                10.0, min(float(os.getenv("CORRECTION_READ_TIMEOUT_SECONDS", "90")), 180.0)
            ),
            correction_max_retries=max(0, min(int(os.getenv("CORRECTION_MAX_RETRIES", "2")), 3)),
            correction_retry_base_seconds=max(
                0.0, min(float(os.getenv("CORRECTION_RETRY_BASE_SECONDS", "1")), 5.0)
            ),
            correction_max_response_bytes=max(
                64 * 1024,
                min(int(os.getenv("CORRECTION_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024))), 2 * 1024 * 1024),
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
