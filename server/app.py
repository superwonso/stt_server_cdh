from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import secrets
import shutil
import threading
import time
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, StrictInt, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from .db import Database
from .importer import ImportDurationError, ImportInterrupted, ImportMediaError, iter_audio_chunks
from .recordings import (
    RecordingCapacityError,
    RecordingConflict,
    RecordingCorruptError,
    RecordingStore,
)
from .security import PASSWORD_HASHER, RateLimiter, digest, new_secret, password_matches
from .settings import Settings
from .transcriber import LocalTranscriber

log = logging.getLogger("classroom")
Username = Annotated[str, StringConstraints(min_length=1, max_length=32)]
Password = Annotated[str, StringConstraints(min_length=4, max_length=128)]


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Username
    setup_code: Annotated[str, StringConstraints(min_length=20, max_length=128)]
    password: Password


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Username
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class LectureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    language: Literal["ko", "en"] | None = "ko"


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    language: Literal["ko", "en"] | None = "ko"
    filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    file_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    size: StrictInt


class DescriptorFileResponse(FileResponse):
    """Serve an already validated Linux descriptor and close it on disconnect."""

    def __init__(self, descriptor: int, **kwargs):
        self.descriptor = descriptor
        super().__init__(f"/proc/self/fd/{descriptor}", **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            descriptor, self.descriptor = self.descriptor, -1
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class BodySizeLimitMiddleware:
    """Bound streamed request bodies before parsers can grow without limit."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                await self._reject(scope, receive, send, 400, "잘못된 요청 크기입니다.")
                return
            if declared < 0 or declared > self.max_bytes:
                await self._reject(scope, receive, send, 413, "요청 크기가 제한을 초과했습니다.")
                return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > self.max_bytes:
                    await self._reject(scope, receive, send, 413, "요청 크기가 제한을 초과했습니다.")
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                disconnected = True
                break

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                if disconnected:
                    return {"type": "http.disconnect"}
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        detail: str,
    ) -> None:
        response = JSONResponse(
            {"detail": detail},
            status_code=status,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
        await response(scope, receive, send)


class RequestBoundaryMiddleware:
    """Reject foreign browser origins before reading a body and harden replies."""

    def __init__(self, app: ASGIApp, allowed_origins: tuple[str, ...]):
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = next(
            (value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"origin"),
            None,
        )
        if origin and origin not in self.allowed_origins:
            response = JSONResponse(
                {"detail": "허용되지 않은 사이트입니다."},
                status_code=403,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
            await response(scope, receive, send)
            return

        async def secure_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, secure_send)


def decode_wav(payload: bytes) -> tuple[np.ndarray, float, bytes]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("Unsupported WAV format")
            frames = audio.getnframes()
            if not 800 <= frames <= 240000:
                raise ValueError("Unsupported audio duration")
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("Truncated WAV")
    except (wave.Error, EOFError, ValueError, OverflowError) as error:
        raise HTTPException(422, "오디오는 0.05~15초의 16kHz 모노 16비트 PCM WAV여야 합니다.") from error
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0, frames / 16000, pcm


def create_app(settings: Settings | None = None, transcriber=None) -> FastAPI:
    settings = settings or Settings.from_env()
    accounts = frozenset(settings.accounts)
    database = Database(settings.database_path, settings.accounts)
    database.initialize()
    engine = transcriber or LocalTranscriber(settings)
    limiter = RateLimiter()
    inference_lock = threading.Lock()
    capacity = threading.BoundedSemaphore(settings.max_pending_chunks)
    dummy_password = PASSWORD_HASHER.hash(new_secret())
    import_directory = settings.data_dir / "imports"
    import_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    import_directory.chmod(0o700)
    recording_store = RecordingStore(
        settings.data_dir / "recordings",
        settings.accounts,
        max_total_bytes=settings.max_recordings_bytes,
        min_free_bytes=settings.recording_free_reserve_bytes,
        max_seconds=settings.max_import_seconds,
        # A user can explicitly save and skip several failed browser chunks.
        # Keep the resulting silence bounded, while allowing the remaining
        # in-memory queue to continue into the same recording.
        max_gap_seconds=180,
    )
    download_ticket_lock = threading.Lock()
    # A small reuse budget lets a browser resume a Range download after a
    # Wi-Fi interruption without ever putting the login bearer in a URL.
    download_tickets: dict[str, tuple[float, str, str, int]] = {}
    if settings.max_upload_bytes < settings.import_part_bytes:
        raise RuntimeError("MAX_UPLOAD_BYTES must be at least the fixed import part size")
    import_part_bytes = settings.import_part_bytes
    import_fs_lock = threading.RLock()
    import_worker_lock = threading.Lock()
    import_worker_wake = threading.Event()
    import_worker_shutdown = threading.Event()
    import_worker_thread: threading.Thread | None = None
    import_current_lock = threading.Lock()
    import_current_id: str | None = None
    import_current_cancel: threading.Event | None = None

    @asynccontextmanager
    async def lifespan(application):
        # Recover interrupted audio only when the single API worker starts;
        # running the account setup CLI must not disturb an active request.
        with database.connect() as connection:
            connection.execute("DELETE FROM chunks WHERE status = 'pending'")
        recover_lecture_deletions()
        recover_import_jobs()
        if settings.model_warmup and hasattr(engine, "warmup"):
            await run_in_threadpool(engine.warmup)
        ensure_import_worker()
        try:
            yield
        finally:
            stop_import_worker()

    app = FastAPI(title="Classroom Transcription", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.transcriber = engine
    app.state.recording_store = recording_store

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Framework validation otherwise echoes passwords and invitation codes.
        return JSONResponse({"detail": "입력값의 형식과 길이를 확인하세요. 새 비밀번호는 4~128자입니다."}, status_code=422)

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.site_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Chunk-Id",
            "X-Start-Seconds",
            "X-Overlap-Seconds",
            "X-Final-Chunk",
            "X-Lecture-Id",
            "X-Import-Id",
            "X-Upload-Offset",
            "X-Part-SHA256",
        ],
        expose_headers=["Retry-After"],
        max_age=600,
    )
    # add_middleware inserts at the front, so this guard is the outermost
    # application middleware and rejects foreign origins before body reads.
    app.add_middleware(RequestBoundaryMiddleware, allowed_origins=settings.site_origins)

    def auth_limit(request: Request, username: str, operation: str):
        address = request.client.host if request.client else "unknown"
        account = username if username in accounts else "unknown"
        if not limiter.allow((operation, address), 30, 300) or not limiter.allow(
            (operation, address, account), 10, 300
        ):
            raise HTTPException(429, "로그인 시도가 많습니다. 5분 후 다시 시도하세요.", headers={"Retry-After": "300"})
        if not limiter.allow((operation, "all-addresses", account), 50, 1800):
            raise HTTPException(429, "로그인 시도가 많습니다. 30분 후 다시 시도하세요.", headers={"Retry-After": "1800"})

    def issue_session(username: str) -> dict:
        if username not in accounts:
            raise RuntimeError("Cannot issue a session for an unknown account")
        token = new_secret()
        now = time.time()
        with database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (digest(token), username, now + settings.session_hours * 3600, now),
            )
            connection.execute(
                "DELETE FROM sessions WHERE username = ? AND token_hash NOT IN "
                "(SELECT token_hash FROM sessions WHERE username = ? ORDER BY created_at DESC LIMIT 5)",
                (username, username),
            )
        return {"token": token, "user": {"username": username}}

    def identity(authorization: str | None = Header(default=None)) -> dict:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 200:
            raise HTTPException(401, "로그인이 필요합니다.", headers={"WWW-Authenticate": "Bearer"})
        token_hash = digest(token)
        with database.connect() as connection:
            session = connection.execute(
                "SELECT username FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, time.time())
            ).fetchone()
        if session is None or session["username"] not in accounts:
            raise HTTPException(401, "로그인이 만료되었습니다. 다시 로그인하세요.", headers={"WWW-Authenticate": "Bearer"})
        return {"username": session["username"], "token_hash": token_hash}

    def owned_lecture(lecture_id: str, username: str, *, include_deleting: bool = False) -> dict:
        deleting_clause = "" if include_deleting else " AND deleting = 0"
        with database.connect() as connection:
            lecture = connection.execute(
                "SELECT id, username, title, language, created_at, deleting, recording_finalized FROM lectures "
                f"WHERE id = ? AND username = ?{deleting_clause}",
                (lecture_id, username),
            ).fetchone()
        if lecture is None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        return dict(lecture)

    def lecture_result(lecture: dict, *, segments: list[dict] | None = None) -> dict:
        result = {key: lecture[key] for key in ("id", "title", "language", "created_at")}
        result["recording_available"] = recording_store.available(lecture["username"], lecture["id"])
        result["recording_finalized"] = bool(lecture["recording_finalized"])
        if segments is not None:
            result["segments"] = segments
        return result

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/auth/activate")
    def activate(body: ActivateBody, request: Request):
        auth_limit(request, body.username, "activate")
        now = time.time()
        code_hash = digest(body.setup_code)
        with database.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
        if body.username not in accounts or user is None or user["password_hash"] or not user["setup_hash"] or not secrets.compare_digest(user["setup_hash"], code_hash) or (user["setup_expires"] or 0) <= now:
            raise HTTPException(400, "초대가 만료되었거나 이미 사용되었습니다.")
        encoded = PASSWORD_HASHER.hash(body.password)
        with database.connect() as connection:
            result = connection.execute(
                "UPDATE users SET password_hash = ?, setup_hash = NULL, setup_expires = NULL "
                "WHERE username = ? AND password_hash IS NULL AND setup_hash = ? AND setup_expires > ?",
                (encoded, body.username, code_hash, time.time()),
            )
            if result.rowcount != 1:
                raise HTTPException(400, "초대가 만료되었거나 이미 사용되었습니다.")
        return issue_session(body.username)

    @app.post("/auth/login")
    def login(body: LoginBody, request: Request):
        auth_limit(request, body.username, "login")
        with database.connect() as connection:
            user = connection.execute("SELECT password_hash FROM users WHERE username = ?", (body.username,)).fetchone()
        encoded = user["password_hash"] if user and user["password_hash"] else dummy_password
        valid_password = password_matches(encoded, body.password)
        if body.username not in accounts or not valid_password or user is None or not user["password_hash"]:
            raise HTTPException(401, "아이디 또는 비밀번호를 확인하세요.")
        return issue_session(body.username)

    @app.get("/auth/me")
    def me(user: dict = Depends(identity)):
        return {"username": user["username"]}

    @app.post("/auth/logout")
    def logout(user: dict = Depends(identity)):
        with database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (user["token_hash"],))
        return {"status": "ok"}

    @app.get("/status")
    def status(user: dict = Depends(identity)):
        return engine.status()

    @app.get("/lectures")
    def list_lectures(user: dict = Depends(identity)):
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT id, username, title, language, created_at, deleting, recording_finalized FROM lectures "
                "WHERE username = ? AND deleting = 0 ORDER BY created_at DESC, id DESC",
                (user["username"],),
            ).fetchall()
        return [lecture_result(dict(row)) for row in rows]

    @app.post("/lectures", status_code=201)
    def create_lecture(
        body: LectureBody,
        x_lecture_id: Annotated[str | None, Header(max_length=64)] = None,
        user: dict = Depends(identity),
    ):
        try:
            lecture_id = str(uuid.UUID(x_lecture_id)) if x_lecture_id else str(uuid.uuid4())
        except (ValueError, AttributeError) as error:
            raise HTTPException(422, "수업 ID가 올바르지 않습니다.") from error

        def replay_or_conflict(connection):
            existing = connection.execute(
                "SELECT id, username, title, language, created_at, deleting, recording_finalized "
                "FROM lectures WHERE id = ?",
                (lecture_id,),
            ).fetchone()
            if existing is None:
                return None
            if (
                existing["username"] != user["username"]
                or existing["title"] != body.title
                or existing["language"] != body.language
            ):
                raise HTTPException(409, "같은 수업 ID로 다른 내용을 만들 수 없습니다.")
            if existing["deleting"]:
                raise HTTPException(409, "삭제 중인 수업 ID는 다시 사용할 수 없습니다.")
            return dict(existing)

        with database.connect() as connection:
            replay = replay_or_conflict(connection)
        if replay is not None:
            return lecture_result(replay)
        if not limiter.allow(("lecture", user["username"]), 30, 300):
            raise HTTPException(429, "잠시 후 다시 수업을 생성하세요.", headers={"Retry-After": "300"})
        lecture = {
            "id": lecture_id,
            "title": body.title,
            "language": body.language,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = replay_or_conflict(connection)
            if replay is None:
                connection.execute(
                    "INSERT INTO lectures(id, username, title, language, created_at) VALUES (?, ?, ?, ?, ?)",
                    (lecture["id"], user["username"], lecture["title"], lecture["language"], lecture["created_at"]),
                )
        if replay is not None:
            return lecture_result(replay)
        return lecture_result(
            {**lecture, "username": user["username"], "recording_finalized": 0}
        )

    @app.get("/lectures/{lecture_id}")
    def get_lecture(lecture_id: str, user: dict = Depends(identity)):
        lecture = owned_lecture(lecture_id, user["username"])
        with database.connect() as connection:
            segments = [dict(row) for row in connection.execute(
                "SELECT id, start, end, text FROM segments WHERE lecture_id = ? ORDER BY start, end, id", (lecture_id,)
            ).fetchall()]
        return lecture_result(lecture, segments=segments)

    def clear_expired_download_tickets(now: float) -> None:
        for token_hash, ticket in tuple(download_tickets.items()):
            if ticket[0] <= now:
                download_tickets.pop(token_hash, None)

    @app.post("/lectures/{lecture_id}/recording-download-ticket")
    def create_recording_download_ticket(lecture_id: str, user: dict = Depends(identity)):
        lecture = owned_lecture(lecture_id, user["username"])
        try:
            recording = recording_store.info(user["username"], lecture["id"])
        except RecordingCorruptError as error:
            log.exception("Stored recording is not readable for lecture %s", lecture["id"])
            raise HTTPException(503, "저장된 녹음 파일을 확인하지 못했습니다.") from error
        if recording is None:
            raise HTTPException(404, "이 수업에는 내려받을 녹음이 없습니다.")
        if not lecture["recording_finalized"]:
            raise HTTPException(409, "녹음이 완전히 저장된 뒤 내려받아 주세요.")
        if not limiter.allow(("recording-download", user["username"]), 30, 60):
            raise HTTPException(429, "잠시 후 녹음을 다시 내려받아 주세요.", headers={"Retry-After": "60"})
        ticket = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + 60
        with download_ticket_lock:
            clear_expired_download_tickets(time.monotonic())
            for token_hash, granted in tuple(download_tickets.items()):
                if granted[1:3] == (user["username"], lecture["id"]):
                    download_tickets.pop(token_hash, None)
            download_tickets[digest(ticket)] = (expires_at, user["username"], lecture["id"], 16)
        return {"path": f"/recording-downloads/{ticket}", "expires_in": 60}

    @app.post("/lectures/{lecture_id}/recording-finalize")
    def finalize_received_recording(lecture_id: str, user: dict = Depends(identity)):
        """Close a stopped lesson after its failed final browser chunk was skipped."""

        with recording_store.lock:
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lecture = connection.execute(
                    "SELECT id, username, recording_finalized FROM lectures "
                    "WHERE id = ? AND username = ? AND deleting = 0",
                    (lecture_id, user["username"]),
                ).fetchone()
                if lecture is None:
                    raise HTTPException(404, "수업을 찾을 수 없습니다.")
                active_import = connection.execute(
                    "SELECT 1 FROM imports WHERE lecture_id = ? "
                    "AND status IN ('uploading', 'queued', 'processing')",
                    (lecture_id,),
                ).fetchone()
                pending = connection.execute(
                    "SELECT 1 FROM chunks WHERE lecture_id = ? AND status = 'pending'",
                    (lecture_id,),
                ).fetchone()
                if active_import is not None or pending is not None:
                    raise HTTPException(409, "진행 중인 음성 처리가 끝난 뒤 녹음을 확정하세요.")
                if not lecture["recording_finalized"]:
                    connection.execute(
                        "UPDATE lectures SET recording_finalized = 1 "
                        "WHERE id = ? AND username = ? AND deleting = 0",
                        (lecture_id, user["username"]),
                    )
            available = recording_store.available(user["username"], lecture_id)
        return {"recording_available": available, "recording_finalized": True}

    @app.get("/recording-downloads/{ticket}")
    def download_recording(ticket: str):
        if not 32 <= len(ticket) <= 128:
            raise HTTPException(404, "다운로드 링크가 만료됐습니다.")
        now = time.monotonic()
        with download_ticket_lock:
            clear_expired_download_tickets(now)
            token_hash = digest(ticket)
            granted = download_tickets.get(token_hash)
            if granted is not None and granted[0] > now:
                if granted[3] <= 1:
                    download_tickets.pop(token_hash, None)
                else:
                    download_tickets[token_hash] = (*granted[:3], granted[3] - 1)
        if granted is None or granted[0] <= now:
            raise HTTPException(404, "다운로드 링크가 만료됐습니다.")
        _, username, lecture_id, _ = granted
        lecture = owned_lecture(lecture_id, username)
        if not lecture["recording_finalized"]:
            raise HTTPException(404, "다운로드 링크가 만료됐습니다.")
        try:
            recording = recording_store.open_info(username, lecture_id)
        except RecordingCorruptError as error:
            log.exception("Stored recording is not readable for lecture %s", lecture_id)
            raise HTTPException(503, "저장된 녹음 파일을 확인하지 못했습니다.") from error
        if recording is None:
            raise HTTPException(404, "이 수업에는 내려받을 녹음이 없습니다.")
        filename = "".join(
            "_" if character in '<>:"/\\|?*' or ord(character) < 32 else character
            for character in lecture["title"]
        ).strip(" .")[:100] or "수업-녹음"
        descriptor = recording["descriptor"]
        try:
            return DescriptorFileResponse(
                descriptor,
                media_type="audio/wav",
                filename=f"{filename}.wav",
                stat_result=recording["stat"],
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        except BaseException:
            os.close(descriptor)
            raise

    def replay_result(
        connection,
        lecture_id: str,
        chunk_id: str,
        payload_hash: str,
        start_seconds: float,
        overlap_seconds: float,
        final_chunk: bool,
    ):
        previous = connection.execute(
            "SELECT * FROM chunks WHERE lecture_id = ? AND chunk_id = ?", (lecture_id, chunk_id)
        ).fetchone()
        if previous is None:
            return None
        if (
            previous["payload_hash"] != payload_hash
            or previous["start_seconds"] != start_seconds
            or previous["overlap_seconds"] != overlap_seconds
            or bool(previous["final_chunk"]) != final_chunk
        ):
            raise HTTPException(409, "같은 음성 조각 ID로 다른 내용을 전송할 수 없습니다.")
        if previous["status"] != "done":
            raise HTTPException(409, "이 음성을 이미 처리하고 있습니다.", headers={"Retry-After": "2"})
        segments = [dict(row) for row in connection.execute(
            "SELECT id, start, end, text FROM segments WHERE lecture_id = ? AND chunk_id = ? ORDER BY start, end, id",
            (lecture_id, chunk_id),
        ).fetchall()]
        lecture = connection.execute(
            "SELECT username, recording_finalized FROM lectures WHERE id = ? AND deleting = 0",
            (lecture_id,),
        ).fetchone()
        if lecture is None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        return {
            "segments": segments,
            "processing_seconds": previous["processing_seconds"],
            "_recording_owner": lecture["username"],
            "recording_finalized": bool(lecture["recording_finalized"]),
        }

    def replay_response(lecture_id: str, replay: dict) -> dict:
        # Never wait for the recording filesystem lock while a SQLite
        # connection/transaction is held; DELETE and normal writes use the
        # opposite (filesystem -> SQLite) order.
        return {
            "segments": replay["segments"],
            "processing_seconds": replay["processing_seconds"],
            "recording_available": recording_store.available(replay["_recording_owner"], lecture_id),
            "recording_finalized": replay["recording_finalized"],
        }

    def process_chunk(
        lecture: dict,
        chunk_id: str,
        start_seconds: float,
        overlap_seconds: float,
        final_chunk: bool,
        payload: bytes,
        interrupted=None,
    ):
        samples, duration, pcm = decode_wav(payload)
        if overlap_seconds > duration or (not final_chunk and overlap_seconds >= duration):
            raise HTTPException(422, "겹침 시간이 음성 길이보다 짧아야 합니다.")
        payload_hash = hashlib.sha256(payload).hexdigest()
        lecture_id = lecture["id"]
        with database.connect() as connection:
            replay = replay_result(
                connection, lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, final_chunk
            )
        if replay is not None:
            return replay_response(lecture_id, replay)
        if not capacity.acquire(blocking=False):
            raise HTTPException(429, "음성 처리 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.", headers={"Retry-After": "2"})
        claimed = False
        try:
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = replay_result(
                    connection, lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, final_chunk
                )
                if replay is None:
                    still_owned = connection.execute(
                        "SELECT recording_finalized FROM lectures "
                        "WHERE id = ? AND username = ? AND deleting = 0",
                        (lecture_id, lecture["username"]),
                    ).fetchone()
                    if still_owned is None:
                        raise HTTPException(404, "수업을 찾을 수 없습니다.")
                    if still_owned["recording_finalized"]:
                        raise HTTPException(409, "이미 종료된 수업에는 음성을 더 추가할 수 없습니다.")
                    connection.execute(
                        "INSERT INTO chunks(lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, final_chunk, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, int(final_chunk)),
                    )
                    claimed = True
            if replay is not None:
                return replay_response(lecture_id, replay)
            started = time.perf_counter()
            if interrupted is None:
                inference_lock.acquire()
            else:
                while not inference_lock.acquire(timeout=0.25):
                    if interrupted():
                        raise ImportInterrupted("import stopped while waiting for inference")
                if interrupted():
                    inference_lock.release()
                    raise ImportInterrupted("import stopped before inference")
            try:
                raw_segments = engine.transcribe(samples, lecture["language"], overlap_seconds, final_chunk)
            finally:
                inference_lock.release()
            processing_seconds = round(time.perf_counter() - started, 3)
            segments = []
            for raw in raw_segments:
                begin, end = float(raw["start"]), float(raw["end"])
                text = str(raw["text"]).strip()
                if not text or not math.isfinite(begin) or not math.isfinite(end):
                    continue
                begin = max(0.0, min(begin, duration))
                end = max(begin, min(end, duration))
                if begin == end:
                    continue
                segments.append({"id": str(uuid.uuid4()), "start": round(start_seconds + begin, 3), "end": round(start_seconds + end, 3), "text": text})
            segments.sort(key=lambda segment: (segment["start"], segment["end"], segment["id"]))
            with recording_store.lock:
                with database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    still_owned = connection.execute(
                        "SELECT recording_finalized FROM lectures "
                        "WHERE id = ? AND username = ? AND deleting = 0",
                        (lecture_id, lecture["username"]),
                    ).fetchone()
                    if still_owned is None:
                        raise HTTPException(404, "수업을 찾을 수 없습니다.")
                    if still_owned["recording_finalized"]:
                        raise HTTPException(409, "이미 종료된 수업에는 음성을 더 추가할 수 없습니다.")
                    try:
                        recording_store.write_chunk(
                            lecture["username"],
                            lecture_id,
                            start_seconds=start_seconds,
                            overlap_seconds=overlap_seconds,
                            pcm=pcm,
                        )
                    except RecordingConflict as error:
                        raise HTTPException(409, "녹음 조각의 시간 순서가 기존 파일과 맞지 않습니다.") from error
                    except RecordingCapacityError as error:
                        raise HTTPException(507, "녹음을 보관할 서버 저장 공간이 부족합니다.") from error
                    except RecordingCorruptError as error:
                        log.exception("Stored recording is corrupt for lecture %s", lecture_id)
                        raise HTTPException(503, "저장된 녹음 파일을 갱신하지 못했습니다.") from error
                    connection.executemany(
                        "INSERT INTO segments(id, lecture_id, chunk_id, start, end, text) VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (segment["id"], lecture_id, chunk_id, segment["start"], segment["end"], segment["text"])
                            for segment in segments
                        ],
                    )
                    connection.execute(
                        "UPDATE chunks SET status = 'done', processing_seconds = ? "
                        "WHERE lecture_id = ? AND chunk_id = ?",
                        (processing_seconds, lecture_id, chunk_id),
                    )
                    if final_chunk:
                        connection.execute(
                            "UPDATE lectures SET recording_finalized = 1 WHERE id = ?",
                            (lecture_id,),
                        )
            recording_available = recording_store.available(lecture["username"], lecture_id)
            return {
                "segments": segments,
                "processing_seconds": processing_seconds,
                "recording_available": recording_available,
                "recording_finalized": final_chunk,
            }
        except (HTTPException, ImportInterrupted):
            raise
        except Exception as error:
            log.exception("Local transcription failed")
            raise HTTPException(503, "이 PC에서 음성 인식을 실행하지 못했습니다. 서버 상태를 확인하고 다시 시도하세요.", headers={"Retry-After": "5"}) from error
        finally:
            if claimed:
                with database.connect() as connection:
                    connection.execute("DELETE FROM chunks WHERE lecture_id = ? AND chunk_id = ? AND status = 'pending'", (lecture_id, chunk_id))
            capacity.release()

    @app.post("/lectures/{lecture_id}/chunks")
    async def upload_chunk(
        lecture_id: str,
        request: Request,
        x_chunk_id: Annotated[str, Header(max_length=64)],
        x_start_seconds: Annotated[str, Header(max_length=40)],
        x_overlap_seconds: Annotated[str, Header(max_length=40)] = "0",
        x_final_chunk: Annotated[str, Header(max_length=8)] = "true",
        user: dict = Depends(identity),
    ):
        lecture = await run_in_threadpool(owned_lecture, lecture_id, user["username"])
        try:
            chunk_id = str(uuid.UUID(x_chunk_id))
            start_seconds = float(x_start_seconds)
            overlap_seconds = float(x_overlap_seconds)
            if not math.isfinite(start_seconds) or not 0 <= start_seconds <= 86400:
                raise ValueError("Invalid start time")
            if not math.isfinite(overlap_seconds) or not 0 <= overlap_seconds <= 3:
                raise ValueError("Invalid overlap")
            final_value = x_final_chunk.strip().lower()
            if final_value not in {"true", "false"}:
                raise ValueError("Invalid final flag")
            final_chunk = final_value == "true"
        except (ValueError, OverflowError) as error:
            raise HTTPException(422, "음성 ID 또는 시작 시간이 올바르지 않습니다.") from error
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "audio/wav":
            raise HTTPException(415, "audio/wav 형식으로 전송하세요.")
        payload = bytearray()
        async for part in request.stream():
            if len(payload) + len(part) > settings.max_upload_bytes:
                raise HTTPException(413, "음성 조각이 너무 큽니다.")
            payload.extend(part)
        return await run_in_threadpool(
            process_chunk, lecture, chunk_id, start_seconds, overlap_seconds, final_chunk, bytes(payload)
        )

    def now_text() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def import_path(username: str, import_id: str, *, create_parent: bool = False):
        if username not in accounts:
            raise RuntimeError("Unknown import owner")
        account_directory = import_directory / username
        if create_parent:
            account_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            account_directory.chmod(0o700)
        return account_directory / f"{import_id}.upload"

    def fetch_import(import_id: str, username: str | None = None):
        query = "SELECT * FROM imports WHERE id = ?"
        values: tuple = (import_id,)
        if username is not None:
            query += " AND username = ?"
            values += (username,)
        with database.connect() as connection:
            row = connection.execute(query, values).fetchone()
        return dict(row) if row is not None else None

    def owned_import(import_id: str, username: str) -> dict:
        try:
            normalized = str(uuid.UUID(import_id))
        except (ValueError, AttributeError) as error:
            raise HTTPException(404, "파일 변환 작업을 찾을 수 없습니다.") from error
        job = fetch_import(normalized, username)
        if job is None:
            raise HTTPException(404, "파일 변환 작업을 찾을 수 없습니다.")
        return job

    def import_result(job: dict) -> dict:
        return {
            "id": job["id"],
            "lecture_id": job["lecture_id"],
            "title": job["title"],
            "language": job["language"],
            "filename": job["filename"],
            "file_fingerprint": job["file_fingerprint"],
            "status": job["status"],
            "total_bytes": job["total_bytes"],
            "uploaded_bytes": job["uploaded_bytes"],
            "next_offset": job["uploaded_bytes"],
            "part_bytes": import_part_bytes,
            "processed_seconds": job["processed_seconds"],
            "duration_seconds": job["duration_seconds"],
            "error": job["error"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "cancel_requested": bool(job["cancel_requested"]),
            # Never tell the browser that private source media is gone until an
            # unlink (or an already-missing file) has been confirmed.
            "raw_deleted": bool(job["raw_deleted"]),
        }

    def complete_file_fingerprint(path, size: int) -> str:
        """Hash an ordered manifest of every transfer part using bounded RAM."""

        part_count = (size + import_part_bytes - 1) // import_part_bytes
        fingerprint = hashlib.sha256()
        fingerprint.update(
            f"stt-import-fingerprint-v2\0{size}\0{import_part_bytes}\0{part_count}\0".encode("utf-8")
        )
        with path.open("rb") as source:
            remaining = size
            while remaining:
                width = min(import_part_bytes, remaining)
                block = source.read(width)
                if len(block) != width:
                    raise OSError("short fingerprint read")
                fingerprint.update(hashlib.sha256(block).digest())
                remaining -= width
            if source.read(1):
                raise OSError("long fingerprint read")
        return fingerprint.hexdigest()

    def remove_private_upload(job: dict) -> bool:
        try:
            import_path(job["username"], job["id"]).unlink(missing_ok=True)
        except OSError:
            log.exception("Could not remove private import upload %s", job["id"])
            return False
        with database.connect() as connection:
            connection.execute(
                "UPDATE imports SET raw_deleted = 1, updated_at = ? WHERE id = ?",
                (now_text(), job["id"]),
            )
        return True

    def reconcile_terminal_upload(job: dict) -> dict:
        """Return terminal state only after one truthful raw-file cleanup attempt."""

        if job["status"] not in {"completed", "failed", "cancelled"} or job["raw_deleted"]:
            return job
        with import_fs_lock:
            current = fetch_import(job["id"])
            if current is None:
                return job
            if current["status"] in {"completed", "failed", "cancelled"} and not current["raw_deleted"]:
                remove_private_upload(current)
                current = fetch_import(job["id"])
            return current or job

    def purge_download_tickets(lecture_id: str) -> None:
        with download_ticket_lock:
            for token_hash, ticket in tuple(download_tickets.items()):
                if ticket[2] == lecture_id:
                    download_tickets.pop(token_hash, None)

    def finalize_lecture_deletion(lecture_id: str, username: str) -> bool:
        """Remove private audio before committing the final metadata deletion."""

        try:
            recording_store.delete(username, lecture_id)
        except (OSError, RecordingCorruptError):
            log.exception("Could not remove private recording for lecture %s", lecture_id)
            return False
        with database.connect() as connection:
            connection.execute(
                "DELETE FROM lectures WHERE id = ? AND username = ? AND deleting = 1",
                (lecture_id, username),
            )
        purge_download_tickets(lecture_id)
        return True

    def recover_lecture_deletions() -> None:
        """Finish durable deletions and remove UUID recordings with no DB owner."""

        with recording_store.lock:
            with database.connect() as connection:
                deleting = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id, username FROM lectures WHERE deleting = 1"
                    ).fetchall()
                ]
            for lecture in deleting:
                finalize_lecture_deletion(lecture["id"], lecture["username"])
            with database.connect() as connection:
                expected = {username: set() for username in settings.accounts}
                for row in connection.execute("SELECT id, username FROM lectures").fetchall():
                    expected[row["username"]].add(row["id"])
            try:
                recording_store.remove_orphans(expected)
            except (OSError, RecordingCorruptError):
                log.exception("Could not remove an orphaned private recording")

    @app.delete("/lectures/{lecture_id}")
    def delete_lecture(lecture_id: str, user: dict = Depends(identity)):
        with import_fs_lock, recording_store.lock:
            try:
                lecture = owned_lecture(lecture_id, user["username"], include_deleting=True)
            except HTTPException as error:
                if error.status_code == 404:
                    # DELETE is idempotent across a lost Quick Tunnel response.
                    # The same response for absent and other-owned UUIDs keeps
                    # another account's lesson existence private.
                    return {"status": "deleted"}
                raise
            if lecture["deleting"]:
                if not finalize_lecture_deletion(lecture["id"], user["username"]):
                    raise HTTPException(503, "녹음 파일 삭제를 완료하지 못했습니다. 잠시 후 다시 시도하세요.")
                return {"status": "deleted"}

            with database.connect() as connection:
                active = connection.execute(
                    "SELECT 1 FROM imports WHERE lecture_id = ? "
                    "AND status IN ('uploading', 'queued', 'processing')",
                    (lecture["id"],),
                ).fetchone()
                pending = connection.execute(
                    "SELECT 1 FROM chunks WHERE lecture_id = ? AND status = 'pending'",
                    (lecture["id"],),
                ).fetchone()
                terminal_jobs = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM imports WHERE lecture_id = ?",
                        (lecture["id"],),
                    ).fetchall()
                ]
            if active is not None or pending is not None:
                raise HTTPException(409, "진행 중인 음성 처리가 끝난 뒤 수업을 삭제하세요.")
            for job in terminal_jobs:
                if not remove_private_upload(job):
                    raise HTTPException(503, "업로드 원본 삭제를 완료하지 못했습니다. 잠시 후 다시 시도하세요.")

            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT deleting FROM lectures WHERE id = ? AND username = ?",
                    (lecture["id"], user["username"]),
                ).fetchone()
                if current is None:
                    raise HTTPException(404, "수업을 찾을 수 없습니다.")
                if not current["deleting"]:
                    active = connection.execute(
                        "SELECT 1 FROM imports WHERE lecture_id = ? "
                        "AND status IN ('uploading', 'queued', 'processing')",
                        (lecture["id"],),
                    ).fetchone()
                    pending = connection.execute(
                        "SELECT 1 FROM chunks WHERE lecture_id = ? AND status = 'pending'",
                        (lecture["id"],),
                    ).fetchone()
                    if active is not None or pending is not None:
                        raise HTTPException(409, "진행 중인 음성 처리가 끝난 뒤 수업을 삭제하세요.")
                    connection.execute("DELETE FROM imports WHERE lecture_id = ?", (lecture["id"],))
                    connection.execute("UPDATE lectures SET deleting = 1 WHERE id = ?", (lecture["id"],))
            purge_download_tickets(lecture["id"])
            if not finalize_lecture_deletion(lecture["id"], user["username"]):
                raise HTTPException(503, "녹음 파일 삭제를 완료하지 못했습니다. 잠시 후 다시 시도하세요.")
        return {"status": "deleted"}

    def abandon_import(import_id: str, status: str, message: str | None) -> None:
        """Finish a failed/cancelled job and remove its partial lecture."""

        with import_fs_lock:
            job = fetch_import(import_id)
            if job is None:
                return
            lecture_id = None
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
                if current is None or current["status"] == "completed":
                    return
                connection.execute(
                    "UPDATE imports SET status = ?, cancel_requested = 0, error = ?, updated_at = ? WHERE id = ?",
                    (status, message, now_text(), import_id),
                )
                if current["lecture_id"]:
                    lecture_id = current["lecture_id"]
                    connection.execute(
                        "UPDATE lectures SET deleting = 1 WHERE id = ? AND username = ?",
                        (lecture_id, current["username"]),
                    )
            if lecture_id:
                finalize_lecture_deletion(lecture_id, job["username"])
            remove_private_upload(job)

    def maintain_import_jobs() -> None:
        """Expire abandoned uploads and retry truthful terminal-file cleanup."""

        stale_before = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        with import_fs_lock:
            with database.connect() as connection:
                stale_ids = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM imports WHERE status = 'uploading' AND updated_at < ?",
                        (stale_before,),
                    ).fetchall()
                ]
            for import_id in stale_ids:
                abandon_import(
                    import_id,
                    "failed",
                    "7일 동안 완료되지 않아 임시 파일을 삭제했습니다. 다시 올려 주세요.",
                )
            with database.connect() as connection:
                terminal = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM imports WHERE status IN ('completed', 'failed', 'cancelled') "
                        "AND raw_deleted = 0"
                    ).fetchall()
                ]
            for job in terminal:
                remove_private_upload(job)

    def recover_import_jobs() -> None:
        """Make an interrupted worker resumable and clean terminal raw files."""

        with import_fs_lock:
            cancelled_lectures: list[tuple[str, str]] = []
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cancelled = connection.execute(
                    "SELECT id, lecture_id, username FROM imports "
                    "WHERE status = 'processing' AND cancel_requested = 1"
                ).fetchall()
                for row in cancelled:
                    connection.execute(
                        "UPDATE imports SET status = 'cancelled', cancel_requested = 0, "
                        "error = NULL, updated_at = ? WHERE id = ?",
                        (now_text(), row["id"]),
                    )
                    if row["lecture_id"]:
                        connection.execute(
                            "UPDATE lectures SET deleting = 1 WHERE id = ? AND username = ?",
                            (row["lecture_id"], row["username"]),
                        )
                        cancelled_lectures.append((row["lecture_id"], row["username"]))
                connection.execute(
                    "UPDATE imports SET status = 'queued', updated_at = ? "
                    "WHERE status = 'processing' AND cancel_requested = 0",
                    (now_text(),),
                )
                rows = [dict(row) for row in connection.execute("SELECT * FROM imports").fetchall()]

            for lecture_id, username in cancelled_lectures:
                finalize_lecture_deletion(lecture_id, username)

            for job in rows:
                path = import_path(job["username"], job["id"], create_parent=True)
                if job["status"] in {"completed", "failed", "cancelled"}:
                    continue
                if path.is_symlink() or not path.is_file():
                    abandon_import(job["id"], "failed", "임시 업로드 파일을 찾을 수 없어 다시 올려야 합니다.")
                    continue
                path.chmod(0o600)
                size = path.stat().st_size
                if job["status"] == "uploading" and size > job["uploaded_bytes"]:
                    # The bytes reached disk before a crash, but the SQLite
                    # offset did not.  Discard only that uncommitted suffix.
                    with path.open("r+b") as output:
                        output.truncate(job["uploaded_bytes"])
                        output.flush()
                        os.fsync(output.fileno())
                    size = job["uploaded_bytes"]
                expected = job["total_bytes"] if job["status"] == "queued" else job["uploaded_bytes"]
                if size != expected:
                    abandon_import(job["id"], "failed", "임시 업로드 파일이 손상되어 다시 올려야 합니다.")

            maintain_import_jobs()

            with database.connect() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM imports").fetchall()]

            expected_uploads = {
                import_path(job["username"], job["id"])
                for job in rows
                if job["status"] in {"uploading", "queued", "processing"}
            }
            for username in settings.accounts:
                account_directory = import_directory / username
                if not account_directory.is_dir():
                    continue
                for candidate in account_directory.glob("*.upload"):
                    if candidate not in expected_uploads:
                        candidate.unlink(missing_ok=True)
            recover_lecture_deletions()

    def import_was_cancelled(import_id: str) -> bool:
        job = fetch_import(import_id)
        return job is None or bool(job["cancel_requested"]) or job["status"] == "cancelled"

    def run_import_job(job: dict, cancel_event: threading.Event) -> None:
        path = import_path(job["username"], job["id"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != job["total_bytes"]:
            abandon_import(job["id"], "failed", "업로드 파일이 완전하지 않아 다시 올려야 합니다.")
            return
        lecture_id = job["lecture_id"]
        if not lecture_id:
            abandon_import(job["id"], "failed", "변환 결과를 저장할 수업 기록이 없습니다.")
            return
        lecture = {
            "id": lecture_id,
            "username": job["username"],
            "title": job["title"],
            "language": job["language"],
            "created_at": job["created_at"],
        }

        def interrupted() -> bool:
            return import_worker_shutdown.is_set() or cancel_event.is_set()

        duration = 0.0
        try:
            if import_was_cancelled(job["id"]):
                cancel_event.set()
                raise ImportInterrupted("cancelled before decode")
            def note_duration(value: float) -> None:
                with database.connect() as connection:
                    connection.execute(
                        "UPDATE imports SET duration_seconds = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'processing'",
                        (round(value, 3), now_text(), job["id"]),
                    )

            for chunk in iter_audio_chunks(
                path,
                max_seconds=settings.max_import_seconds,
                interrupted=interrupted,
                on_duration=note_duration,
            ):
                if interrupted():
                    raise ImportInterrupted("interrupted before transcription")
                chunk_id = str(uuid.uuid5(uuid.UUID(job["id"]), f"audio-chunk:{chunk.index}"))
                while True:
                    try:
                        process_chunk(
                            lecture,
                            chunk_id,
                            chunk.start_seconds,
                            chunk.overlap_seconds,
                            chunk.final,
                            chunk.payload,
                            interrupted=interrupted,
                        )
                        break
                    except HTTPException as error:
                        if error.status_code != 429:
                            raise
                        if interrupted():
                            raise ImportInterrupted("interrupted while waiting for inference") from error
                        time.sleep(0.25)
                duration = max(duration, chunk.start_seconds + chunk.duration_seconds)
                with database.connect() as connection:
                    connection.execute(
                        "UPDATE imports SET processed_seconds = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'processing'",
                        (round(duration, 3), now_text(), job["id"]),
                    )
            with database.connect() as connection:
                result = connection.execute(
                    "UPDATE imports SET status = 'completed', duration_seconds = ?, processed_seconds = ?, "
                    "cancel_requested = 0, error = NULL, updated_at = ? "
                    "WHERE id = ? AND status = 'processing' AND cancel_requested = 0",
                    (round(duration, 3), round(duration, 3), now_text(), job["id"]),
                )
            if result.rowcount == 1:
                remove_private_upload(job)
            else:
                abandon_import(job["id"], "cancelled", None)
        except ImportInterrupted:
            if import_was_cancelled(job["id"]):
                abandon_import(job["id"], "cancelled", None)
            else:
                # Server shutdown is not a failed conversion.  The deterministic
                # chunk IDs make replay safe when the next process starts.
                with database.connect() as connection:
                    connection.execute(
                        "UPDATE imports SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'processing'",
                        (now_text(), job["id"]),
                    )
        except ImportDurationError:
            abandon_import(
                job["id"],
                "failed",
                f"오디오는 최대 {settings.max_import_seconds // 60}분까지 변환할 수 있습니다.",
            )
        except ImportMediaError:
            abandon_import(job["id"], "failed", "지원되는 오디오가 있는 파일인지 확인해 주세요.")
        except HTTPException:
            log.exception("Transcription failed for import %s", job["id"])
            abandon_import(job["id"], "failed", "이 PC에서 파일을 변환하지 못했습니다. 서버 상태를 확인해 주세요.")
        except Exception:
            log.exception("Unexpected file import failure %s", job["id"])
            abandon_import(job["id"], "failed", "파일 변환 중 서버 오류가 발생했습니다.")

    def import_worker_main() -> None:
        nonlocal import_current_id, import_current_cancel
        next_maintenance = 0.0
        while not import_worker_shutdown.is_set():
            claimed_id = None
            try:
                import_worker_wake.clear()
                if time.monotonic() >= next_maintenance:
                    maintain_import_jobs()
                    next_maintenance = time.monotonic() + 60
                with import_fs_lock:
                    with database.connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        row = connection.execute(
                            "SELECT * FROM imports WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
                        ).fetchone()
                        if row is not None:
                            claimed = connection.execute(
                                "UPDATE imports SET status = 'processing', updated_at = ? "
                                "WHERE id = ? AND status = 'queued'",
                                (now_text(), row["id"]),
                            ).rowcount
                            row = dict(row) if claimed == 1 else None
                            claimed_id = row["id"] if row is not None else None
                if row is None:
                    import_worker_wake.wait(1.0)
                    continue
                cancel_event = threading.Event()
                with import_current_lock:
                    import_current_id = row["id"]
                    import_current_cancel = cancel_event
                try:
                    refreshed = fetch_import(row["id"])
                    if refreshed is not None:
                        run_import_job(refreshed, cancel_event)
                    claimed_id = None
                finally:
                    with import_current_lock:
                        import_current_id = None
                        import_current_cancel = None
            except Exception:
                # A transient SQLite/filesystem error must not silently kill the
                # only background worker and strand all queued private imports.
                log.exception("File import worker loop failed; retrying")
                if claimed_id is not None:
                    try:
                        with database.connect() as connection:
                            connection.execute(
                                "UPDATE imports SET status = 'queued', updated_at = ? "
                                "WHERE id = ? AND status = 'processing'",
                                (now_text(), claimed_id),
                            )
                    except Exception:
                        log.exception("Could not requeue import %s after worker-loop failure", claimed_id)
                    import_worker_wake.set()
                import_worker_wake.wait(1.0)

    def ensure_import_worker() -> None:
        nonlocal import_worker_thread
        with import_worker_lock:
            if import_worker_shutdown.is_set() or (import_worker_thread and import_worker_thread.is_alive()):
                return
            import_worker_thread = threading.Thread(
                target=import_worker_main,
                name="recording-import-worker",
                daemon=True,
            )
            import_worker_thread.start()

    def stop_import_worker() -> None:
        import_worker_shutdown.set()
        import_worker_wake.set()
        with import_worker_lock:
            worker = import_worker_thread
        if worker and worker.is_alive():
            # stop.sh grants the API process 20 seconds before its final
            # SIGKILL fallback. Leave a margin for uvicorn to close sockets and
            # logs; a still-running job remains `processing` with its raw file
            # and is reset to `queued` on the next startup.
            worker.join(timeout=15)
            if worker.is_alive():
                log.warning("File import worker did not stop within 15 seconds; its raw upload is retained")

    app.state.stop_import_worker = stop_import_worker

    @app.post("/imports", status_code=201)
    def create_import(
        body: ImportBody,
        x_import_id: Annotated[str | None, Header(max_length=64)] = None,
        user: dict = Depends(identity),
    ):
        ensure_import_worker()
        maintain_import_jobs()
        if not 1 <= body.size <= settings.max_import_bytes:
            raise HTTPException(422, f"파일은 1바이트 이상 {settings.max_import_bytes}바이트 이하여야 합니다.")
        filename = body.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not filename or any(ord(character) < 32 for character in filename):
            raise HTTPException(422, "파일 이름을 확인하세요.")
        try:
            import_id = str(uuid.UUID(x_import_id)) if x_import_id else str(uuid.uuid4())
        except (ValueError, AttributeError) as error:
            raise HTTPException(422, "파일 변환 ID가 올바르지 않습니다.") from error
        lecture_id = str(uuid.uuid5(uuid.UUID(import_id), "lecture"))
        created = now_text()
        path = import_path(user["username"], import_id, create_parent=True)
        made_file = False
        with import_fs_lock:
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
                if existing is not None:
                    if (
                        existing["username"] != user["username"]
                        or existing["title"] != body.title
                        or existing["language"] != body.language
                        or existing["filename"] != filename
                        or existing["file_fingerprint"] != body.file_fingerprint
                        or existing["total_bytes"] != body.size
                    ):
                        raise HTTPException(409, "같은 파일 변환 ID로 다른 작업을 만들 수 없습니다.")
                    return import_result(dict(existing))
                active = connection.execute(
                    "SELECT id FROM imports WHERE username = ? AND status IN ('uploading', 'queued', 'processing')",
                    (user["username"],),
                ).fetchone()
                if active is not None:
                    raise HTTPException(409, "먼저 진행 중인 파일 변환을 완료하거나 취소하세요.")
                reserved = connection.execute(
                    "SELECT COALESCE(SUM(total_bytes - uploaded_bytes), 0) FROM imports "
                    "WHERE status IN ('uploading', 'queued', 'processing')"
                ).fetchone()[0]
                free = shutil.disk_usage(import_directory).free
                if body.size + reserved + 256 * 1024 * 1024 > free:
                    raise HTTPException(507, "서버 저장 공간이 부족합니다.")
                try:
                    recording_store.ensure_capacity(
                        settings.max_import_seconds * 16_000 * 2,
                        other_reserved_bytes=body.size + reserved,
                    )
                except RecordingCapacityError as error:
                    raise HTTPException(507, "녹음과 변환 파일을 보관할 서버 저장 공간이 부족합니다.") from error
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(descriptor)
                    made_file = True
                    connection.execute(
                        "INSERT INTO lectures(id, username, title, language, created_at) VALUES (?, ?, ?, ?, ?)",
                        (lecture_id, user["username"], body.title, body.language, created),
                    )
                    connection.execute(
                        "INSERT INTO imports(id, username, lecture_id, title, language, filename, file_fingerprint, total_bytes, "
                        "uploaded_bytes, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'uploading', ?, ?)",
                        (
                            import_id,
                            user["username"],
                            lecture_id,
                            body.title,
                            body.language,
                            filename,
                            body.file_fingerprint,
                            body.size,
                            created,
                            created,
                        ),
                    )
                except BaseException:
                    if made_file:
                        path.unlink(missing_ok=True)
                    raise
        return import_result(owned_import(import_id, user["username"]))

    @app.get("/imports")
    def list_imports(user: dict = Depends(identity)):
        ensure_import_worker()
        maintain_import_jobs()
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM imports WHERE username = ? "
                "ORDER BY CASE status WHEN 'uploading' THEN 0 WHEN 'queued' THEN 0 WHEN 'processing' THEN 0 ELSE 1 END, "
                "created_at DESC, id DESC LIMIT 20",
                (user["username"],),
            ).fetchall()
        return [import_result(dict(row)) for row in rows]

    @app.get("/imports/{import_id}")
    def get_import(import_id: str, user: dict = Depends(identity)):
        ensure_import_worker()
        job = owned_import(import_id, user["username"])
        return import_result(reconcile_terminal_upload(job))

    @app.put("/imports/{import_id}")
    async def upload_import_part(
        import_id: str,
        request: Request,
        x_upload_offset: Annotated[str, Header(max_length=32)],
        x_part_sha256: Annotated[str, Header(max_length=64)],
        user: dict = Depends(identity),
    ):
        job = await run_in_threadpool(owned_import, import_id, user["username"])
        try:
            offset = int(x_upload_offset)
            if str(offset) != x_upload_offset.strip() or offset < 0:
                raise ValueError
        except (ValueError, OverflowError) as error:
            raise HTTPException(422, "업로드 위치가 올바르지 않습니다.") from error
        supplied_hash = x_part_sha256.strip().lower()
        if len(supplied_hash) != 64 or any(character not in "0123456789abcdef" for character in supplied_hash):
            raise HTTPException(422, "파일 조각 해시가 올바르지 않습니다.")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise HTTPException(415, "application/octet-stream 형식으로 전송하세요.")
        payload = bytearray()
        async for part in request.stream():
            if len(payload) + len(part) > import_part_bytes:
                raise HTTPException(413, "파일 조각이 너무 큽니다.")
            payload.extend(part)
        payload = bytes(payload)
        if not payload:
            raise HTTPException(422, "빈 파일 조각은 전송할 수 없습니다.")
        if not secrets.compare_digest(hashlib.sha256(payload).hexdigest(), supplied_hash):
            raise HTTPException(422, "파일 조각이 전송 중 변경되었습니다.")

        with import_fs_lock:
            job = owned_import(job["id"], user["username"])
            if job["status"] != "uploading":
                raise HTTPException(409, "이 파일은 더 이상 업로드할 수 없습니다.")
            expected = job["uploaded_bytes"]
            if offset > expected or offset + len(payload) > job["total_bytes"]:
                raise HTTPException(409, f"파일 업로드 위치가 다릅니다. {expected}바이트부터 다시 보내세요.")
            if offset + len(payload) < job["total_bytes"] and len(payload) != import_part_bytes:
                raise HTTPException(422, f"마지막 조각 외에는 {import_part_bytes}바이트로 전송하세요.")
            path = import_path(job["username"], job["id"])
            if path.is_symlink() or not path.is_file():
                abandon_import(job["id"], "failed", "임시 업로드 파일을 찾을 수 없어 다시 올려야 합니다.")
                raise HTTPException(409, "임시 업로드 파일이 없어 새 작업으로 다시 올려야 합니다.")
            actual_size = path.stat().st_size
            if actual_size > expected:
                with path.open("r+b") as output:
                    output.truncate(expected)
                    output.flush()
                    os.fsync(output.fileno())
                actual_size = expected
            if actual_size < expected:
                abandon_import(job["id"], "failed", "임시 업로드 파일이 손상되어 다시 올려야 합니다.")
                raise HTTPException(409, "임시 업로드 파일이 손상되어 새 작업으로 다시 올려야 합니다.")
            if offset < expected:
                if offset + len(payload) > expected:
                    raise HTTPException(409, f"파일 업로드 위치가 다릅니다. {expected}바이트부터 다시 보내세요.")
                with path.open("rb") as source:
                    source.seek(offset)
                    previous = source.read(len(payload))
                if not secrets.compare_digest(previous, payload):
                    raise HTTPException(409, "이미 받은 위치에 다른 파일 조각을 보낼 수 없습니다.")
                return import_result(job)
            with path.open("r+b") as output:
                output.seek(offset)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            with database.connect() as connection:
                changed = connection.execute(
                    "UPDATE imports SET uploaded_bytes = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'uploading' AND uploaded_bytes = ?",
                    (offset + len(payload), now_text(), job["id"], offset),
                ).rowcount
            if changed != 1:
                raise HTTPException(409, "파일 업로드 상태가 바뀌었습니다. 상태를 다시 확인하세요.")
        return import_result(owned_import(job["id"], user["username"]))

    @app.post("/imports/{import_id}/complete")
    def complete_import(import_id: str, user: dict = Depends(identity)):
        with import_fs_lock:
            job = owned_import(import_id, user["username"])
            if job["status"] in {"queued", "processing", "completed"}:
                result = import_result(reconcile_terminal_upload(job))
            elif job["status"] != "uploading":
                raise HTTPException(409, "완료할 수 없는 파일 변환 작업입니다.")
            else:
                path = import_path(job["username"], job["id"])
                if (
                    job["uploaded_bytes"] != job["total_bytes"]
                    or path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size != job["total_bytes"]
                ):
                    raise HTTPException(409, "파일 업로드가 아직 끝나지 않았습니다.")
                try:
                    fingerprint = complete_file_fingerprint(path, job["total_bytes"])
                except OSError:
                    abandon_import(job["id"], "failed", "업로드 파일을 다시 확인할 수 없어 새 작업으로 올려야 합니다.")
                    raise HTTPException(409, "업로드 파일을 확인하지 못했습니다. 새 작업으로 다시 올려 주세요.")
                if not secrets.compare_digest(fingerprint, job["file_fingerprint"]):
                    abandon_import(job["id"], "failed", "선택한 파일 조각이 서로 달라 변환을 시작하지 않았습니다.")
                    raise HTTPException(409, "선택한 파일이 업로드 도중 바뀌었습니다. 새 작업으로 다시 올려 주세요.")
                with database.connect() as connection:
                    changed = connection.execute(
                        "UPDATE imports SET status = 'queued', updated_at = ? "
                        "WHERE id = ? AND status = 'uploading' AND uploaded_bytes = total_bytes",
                        (now_text(), job["id"]),
                    ).rowcount
                if changed != 1:
                    raise HTTPException(409, "파일 변환 상태가 바뀌었습니다. 상태를 다시 확인하세요.")
                result = import_result(owned_import(job["id"], user["username"]))
        ensure_import_worker()
        import_worker_wake.set()
        return result

    @app.post("/imports/{import_id}/cancel")
    def cancel_import(import_id: str, user: dict = Depends(identity)):
        with import_fs_lock:
            job = owned_import(import_id, user["username"])
            if job["status"] in {"completed", "failed", "cancelled"}:
                return import_result(reconcile_terminal_upload(job))
            if job["status"] == "processing":
                with database.connect() as connection:
                    connection.execute(
                        "UPDATE imports SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status = 'processing'",
                        (now_text(), job["id"]),
                    )
                with import_current_lock:
                    if import_current_id == job["id"] and import_current_cancel is not None:
                        import_current_cancel.set()
                result = import_result(owned_import(job["id"], user["username"]))
            else:
                lecture_id = None
                with database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute("SELECT * FROM imports WHERE id = ?", (job["id"],)).fetchone()
                    connection.execute(
                        "UPDATE imports SET status = 'cancelled', cancel_requested = 0, error = NULL, updated_at = ? "
                        "WHERE id = ? AND status IN ('uploading', 'queued')",
                        (now_text(), job["id"]),
                    )
                    if current and current["lecture_id"]:
                        lecture_id = current["lecture_id"]
                        connection.execute(
                            "UPDATE lectures SET deleting = 1 WHERE id = ? AND username = ?",
                            (lecture_id, current["username"]),
                        )
                if lecture_id:
                    finalize_lecture_deletion(lecture_id, user["username"])
                remove_private_upload(job)
                result = import_result(owned_import(job["id"], user["username"]))
        import_worker_wake.set()
        return result

    return app
