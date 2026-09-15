"""Owner-scoped, restart-safe lecture translations; no audio/inference locks."""
from __future__ import annotations

import json
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import Depends, HTTPException

from .translator import TranslationError, validate_translation_segments, _raw_segments, MAX_TRANSLATED_CHARS
from .postprocessor import PostprocessingError
from .llm_result import WARNING_CODES, draft_document, safe_draft_text, validate_draft_document


def _now():
    return datetime.now(timezone.utc).isoformat()


def _warning_codes(values):
    if not isinstance(values, (list, tuple)):
        return ['validation_failed']
    return list(dict.fromkeys(value if value in WARNING_CODES else 'validation_failed'
                              for value in values if isinstance(value, str)))


def translation_document(rows, raw, warnings=()):
    """Keep provenance exact while treating result-quality checks as diagnostics."""
    sources = _raw_segments(raw)
    notices = _warning_codes(warnings)
    try:
        validated = validate_translation_segments(rows, sources)
    except TranslationError:
        notices.append('validation_failed')
        validated = None
    if validated is None:
        try:
            if not isinstance(rows, list) or len(rows) != len(sources):
                raise ValueError
            by_id = {}
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in by_id
                        or not isinstance(row.get('text'), str) or not row['text'].strip()):
                    raise ValueError
                by_id[row['id']] = row
            if set(by_id) != {source['id'] for source in sources}:
                raise ValueError
            validated = [{**source, 'text': by_id[source['id']]['text']} for source in sources]
        except (ValueError, TypeError, KeyError):
            return draft_document(rows, warnings=notices, replacements={}, max_chars=MAX_TRANSLATED_CHARS)
    source_by_id = {source['id']: source for source in sources}
    cleaned = []
    try:
        for row in validated:
            # Known restored values were already resolved by the provider.
            # A literal source marker is allowed only if it occurs in that row.
            source = source_by_id[row['id']]
            allowed = {token: token for token in re.findall(r'__(?:PRIVATE|KOREAN)_[0-9]{6}__', source['text'])}
            text = safe_draft_text(row['text'], replacements=allowed, max_chars=250_000)
            if text != row['text']:
                notices.append('validation_failed')
            cleaned.append({**source, 'text': text})
    except ValueError:
        return draft_document(rows, warnings=[*notices, 'validation_failed'], replacements={}, max_chars=MAX_TRANSLATED_CHARS)
    if sum(len(row['text']) for row in cleaned) > MAX_TRANSLATED_CHARS:
        return draft_document(cleaned, warnings=[*notices, 'content_limited'], replacements={}, max_chars=MAX_TRANSLATED_CHARS)
    notices = list(dict.fromkeys(notices))
    return {'format': 'segments', 'segments': cleaned, 'warnings': notices} if notices else cleaned


class TranslationService:
    def __init__(self, settings, database, engine, limiter):
        self.settings, self.database, self.engine, self.limiter = settings, database, engine, limiter
        self.shutdown = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    @property
    def configured(self):
        return bool(getattr(self.engine, "configured", True))

    def install(self, app, *, identity, owned_lecture, raw_segments, transcript_revision):
        self.raw_segments, self.revision = raw_segments, transcript_revision

        @app.get("/lectures/{lecture_id}/translation")
        def get_translation(lecture_id: str, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT s.* FROM lecture_translations s JOIN lectures l ON l.id=s.lecture_id "
                    "WHERE l.id=? AND l.username=? AND l.deleting=0 AND l.trashed_at IS NULL",
                    (lecture_id, user["username"]),
                ).fetchone()
                result = self.result(row, connection)
            return {"configured": self.configured, "model": self.settings.translation_model, "translation": result}

        @app.post("/lectures/{lecture_id}/translation", status_code=202)
        def create_translation(lecture_id: str, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            if not self.configured:
                raise HTTPException(503, "운영자가 수업 번역 API를 설정해야 합니다.")
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lecture = connection.execute(
                    "SELECT * FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
                    (lecture_id, user["username"]),
                ).fetchone()
                if lecture is None:
                    raise HTTPException(404, "수업을 찾을 수 없습니다.")
                if not lecture["recording_finalized"]:
                    raise HTTPException(409, "수업 녹음과 받아쓰기 저장이 끝난 뒤 번역을 만들 수 있습니다.")
                segments = self.raw_segments(connection, lecture_id)
                if not segments or not any(s["text"].strip() for s in segments):
                    raise HTTPException(409, "번역할 받아쓰기 내용이 없습니다.")
                if (len(segments) > 50000 or any(len(s["text"]) > 24000 for s in segments)
                        or sum(len(s["text"]) for s in segments) > self.settings.translation_max_source_chars):
                    raise HTTPException(413, "번역 가능한 원문 분량을 초과했습니다.")
                revision = self.revision(segments)
                existing = connection.execute(
                    "SELECT * FROM lecture_translations WHERE lecture_id=?", (lecture_id,)
                ).fetchone()
                if existing is not None and existing["status"] in ("queued", "processing"):
                    return self.envelope(self.result(existing, connection))
                if (existing is not None and existing["status"] == "completed"
                        and existing["raw_revision"] == revision
                        and existing["model"] == self.settings.translation_model):
                    saved = self.result(existing, connection)
                    if saved["status"] == "completed":
                        return self.envelope(saved)
                active = connection.execute(
                    "SELECT 1 FROM lecture_translations s JOIN lectures l ON l.id=s.lecture_id "
                    "WHERE l.username=? AND l.deleting=0 AND l.trashed_at IS NULL "
                    "AND s.status IN ('queued','processing')", (user["username"],)
                ).fetchone()
                if active is not None:
                    raise HTTPException(409, "진행 중인 수업 번역이 끝난 뒤 다시 시도하세요.")
                if not self.limiter.allow(("lecture-translation", user["username"]), 6, 3600):
                    raise HTTPException(429, "번역 요청이 많습니다. 잠시 후 다시 시도하세요.")
                now = _now()
                connection.execute(
                    "INSERT INTO lecture_translations(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                    "VALUES(?,?,?,'queued',?,?,?) ON CONFLICT(lecture_id) DO UPDATE SET "
                    "job_id=excluded.job_id,raw_revision=excluded.raw_revision,status='queued',model=excluded.model,"
                    "translation_json=NULL,error_code=NULL,error=NULL,attempts=0,created_at=excluded.created_at,"
                    "updated_at=excluded.updated_at,completed_at=NULL",
                    (lecture_id, str(uuid.uuid4()), revision, self.settings.translation_model, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM lecture_translations WHERE lecture_id=?", (lecture_id,)
                ).fetchone()
                result = self.result(row, connection)
            self.start()
            self.wake.set()
            return self.envelope(result)

    def envelope(self, result):
        return {"configured": self.configured, "model": self.settings.translation_model, "translation": result}

    def result(self, row, connection):
        if row is None:
            return None
        result = {key: row[key] for key in (
            "lecture_id", "status", "model", "error_code", "error", "created_at", "updated_at", "completed_at"
        )}
        result["segments"] = []
        if row["status"] == "completed":
            try:
                segments = self.raw_segments(connection, row["lecture_id"])
                if self.revision(segments) != row["raw_revision"]:
                    raise ValueError("stale")
                document = json.loads(row["translation_json"])
                if isinstance(document, dict) and document.get('format') == 'draft':
                    result['document'] = validate_draft_document(document, max_chars=MAX_TRANSLATED_CHARS)
                else:
                    warnings = ()
                    if isinstance(document, dict):
                        if set(document) != {'format', 'segments', 'warnings'} or document['format'] != 'segments':
                            raise ValueError('invalid saved envelope')
                        warnings = document['warnings']
                        if (not isinstance(warnings, list) or not 1 <= len(warnings) <= 16
                                or any(not isinstance(code, str) or code not in WARNING_CODES for code in warnings)
                                or len(set(warnings)) != len(warnings)):
                            raise ValueError('invalid saved warnings')
                        document = document['segments']
                    restored = translation_document(document, segments, warnings)
                    if isinstance(restored, dict):
                        result['document'] = restored
                        result['segments'] = restored.get('segments', [])
                    else:
                        result['segments'] = restored
            except Exception:
                result.update(status="failed", error_code="invalid_saved_translation",
                              error="저장된 번역을 확인하지 못했습니다.")
        return result

    def recover(self):
        with self.database.connect() as connection:
            if not self.configured:
                connection.execute(
                    "UPDATE lecture_translations SET status='failed',error_code='not_configured',"
                    "error='수업 번역 API 키가 서버에 설정되지 않았습니다.',updated_at=? "
                    "WHERE status IN ('queued','processing') AND lecture_id IN "
                    "(SELECT id FROM lectures WHERE deleting=0 AND trashed_at IS NULL)", (_now(),)
                )
                return
            connection.execute(
                "UPDATE lecture_translations SET status='failed',error_code='interrupted',"
                "error='번역이 여러 번 중단되었습니다. 다시 요청하세요.',updated_at=? "
                "WHERE status='processing' AND attempts>=3 AND lecture_id IN "
                "(SELECT id FROM lectures WHERE deleting=0 AND trashed_at IS NULL)", (_now(),)
            )
            connection.execute(
                "UPDATE lecture_translations SET status='queued',updated_at=? WHERE status='processing' "
                "AND lecture_id IN (SELECT id FROM lectures WHERE deleting=0 AND trashed_at IS NULL)", (_now(),)
            )

    def process_next(self):
        if self.shutdown.is_set() or not self.configured:
            return False
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            access = connection.execute("SELECT access_enabled FROM operational_state WHERE singleton=1").fetchone()
            if not access or not access[0]:
                return False
            row = connection.execute(
                "SELECT s.*,l.username,l.language,l.recording_finalized FROM lecture_translations s "
                "JOIN lectures l ON l.id=s.lecture_id WHERE s.status='queued' AND l.deleting=0 AND l.trashed_at IS NULL "
                "ORDER BY s.created_at,s.lecture_id LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            job = dict(row)
            segments = self.raw_segments(connection, job["lecture_id"])
            connection.execute(
                "UPDATE lecture_translations SET status='processing',attempts=attempts+1,updated_at=? WHERE job_id=?",
                (_now(), job["job_id"]),
            )
        document, code, message = None, None, None
        try:
            if (not job["recording_finalized"] or self.revision(segments) != job["raw_revision"]
                    or job["model"] != self.settings.translation_model):
                raise ValueError("stale")
            # The service keeps its own immutable validation snapshot even if
            # an injected engine mutates the list it receives.
            output = self.engine.translate(language=job["language"], segments=deepcopy(segments),
                                           interrupted=self.shutdown.is_set)
            draft = getattr(output, 'draft_text', None)
            warnings = getattr(output, 'warnings', ())
            if draft is not None:
                document = draft_document(draft, warnings=warnings, replacements={}, max_chars=MAX_TRANSLATED_CHARS)
            else:
                document = translation_document(output.segments, segments, warnings)
        except PostprocessingError as error:
            # Rebuild a known code from fixed messages. Never persist a
            # provider-controlled exception string, even on this typed path.
            safe = TranslationError(error.code)
            document, code, message = None, safe.code, safe.message
        except Exception:
            # Never persist provider bodies, keys, raw text, or exception strings.
            document = None
            code, message = "translation_failed", "수업 번역을 완료하지 못했습니다. 잠시 후 다시 요청하세요."
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT l.recording_finalized FROM lecture_translations s JOIN lectures l ON l.id=s.lecture_id "
                "WHERE s.job_id=? AND s.status='processing' AND l.username=? AND l.deleting=0 AND l.trashed_at IS NULL",
                (job["job_id"], job["username"]),
            ).fetchone()
            if current is None:
                return True
            now = _now()
            if self.shutdown.is_set():
                state = "queued" if job["attempts"] + 1 < 3 else "failed"
                connection.execute(
                    "UPDATE lecture_translations SET status=?,error_code='interrupted',"
                    "error='서버 재시작으로 번역이 중단되었습니다.',updated_at=? WHERE job_id=?",
                    (state, now, job["job_id"]),
                )
                return True
            if not current[0] or self.revision(self.raw_segments(connection, job["lecture_id"])) != job["raw_revision"]:
                document, code, message = None, "source_changed", "원문이 변경되어 번역을 저장하지 않았습니다. 다시 요청하세요."
            if document is None:
                connection.execute(
                    "UPDATE lecture_translations SET status='failed',error_code=?,error=?,updated_at=? WHERE job_id=?",
                    (code, message, now, job["job_id"]),
                )
            else:
                connection.execute(
                    "UPDATE lecture_translations SET status='completed',translation_json=?,error_code=NULL,error=NULL,"
                    "updated_at=?,completed_at=? WHERE job_id=?",
                    (json.dumps(document, ensure_ascii=False), now, now, job["job_id"]),
                )
        return True

    def start(self):
        with self.lock:
            if self.shutdown.is_set() or not self.configured or (self.thread is not None and self.thread.is_alive()):
                return
            self.thread = threading.Thread(target=self._run, name="lecture-translation", daemon=True)
            try:
                self.thread.start()
            except Exception:
                # An unstarted Thread cannot be joined during lifespan cleanup.
                self.thread = None
                raise

    def _run(self):
        failure_cleanup = False
        while not self.shutdown.is_set():
            try:
                if failure_cleanup:
                    # Only this worker claims jobs in production. A claim can
                    # outlive process_next() if its final DB write fails. Do
                    # not leave that owner (or deletion) blocked indefinitely,
                    # and do not resend the provider request automatically.
                    # Retry this cleanup before claiming any other work; a
                    # completed commit whose acknowledgement was lost is kept.
                    with self.database.connect() as connection:
                        connection.execute(
                            "UPDATE lecture_translations SET status='failed',"
                            "error_code='translation_save_failed',"
                            "error='수업 번역 결과를 저장하지 못했습니다. 다시 요청하세요.',updated_at=? "
                            "WHERE status='processing'", (_now(),)
                        )
                    failure_cleanup = False
                if self.process_next():
                    continue
            except Exception:
                # The provider never logs its input; worker failures are also
                # deliberately content-free. Startup recovery remains separate
                # for an actual process shutdown before cleanup can finish.
                failure_cleanup = True
            self.wake.wait(1)
            self.wake.clear()

    def request_shutdown(self):
        self.shutdown.set()
        self.wake.set()

    def stop(self, timeout=5):
        self.request_shutdown()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=max(0, timeout))
        stopped = self.thread is None or not self.thread.is_alive()
        if stopped and hasattr(self.engine, "close"):
            self.engine.close()
        return stopped
