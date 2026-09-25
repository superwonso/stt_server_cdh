"""Owner-scoped, chunked private course-material uploads and local conversion.

No upload or read invokes an LLM. Conversion has explicit admission, immutable
input hashes, and a bounded separate process; provider work belongs to notes.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .platform_files import ensure_private_directory, open_file, validate_private_path

MAX_FILE = 32 * 1024 * 1024
MAX_SCOPE_FILES = 20
MAX_OWNER_BYTES = 512 * 1024 * 1024
MAX_PART = 480 * 1024
# List responses must not load every full converted document into Python memory.
MATERIAL_LIST_COLUMNS = "id,course_id,lecture_id,filename,kind,size_bytes,uploaded_bytes,status,revision,created_at,updated_at,error_code,CASE WHEN json_valid(document_json) THEN json_object('unit_count',json_extract(document_json,'$.unit_count'),'warnings',json_extract(document_json,'$.warnings')) ELSE NULL END AS document_json"
SAFE_ERRORS = {
    "awaiting_upload": "자료 파일을 전송해 주세요.",
    "converting": "페이지와 슬라이드를 Markdown으로 변환하고 있습니다.",
    "interrupted": "자료 변환이 중단되었습니다. 원본을 확인하고 다시 변환해 주세요.",
    "invalid_file": "자료 파일을 읽지 못했습니다. PDF 또는 PPTX 파일을 확인해 주세요.",
    "limits_exceeded": "자료가 변환 크기·시간 제한을 넘었습니다. 파일을 나누어 올려 주세요.",
    "dependency_unavailable": "서버의 자료 변환 도구가 준비되지 않았습니다.",
    "conversion_failed": "자료를 변환하지 못했습니다. 원본은 보관되어 있습니다.",
    "source_changed": "수업 또는 강의 상태가 바뀌어 변환 결과를 저장하지 않았습니다.",
}


def now():
    return datetime.now(timezone.utc).isoformat()


class MaterialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    filename: str = Field(min_length=1, max_length=180)
    size_bytes: int = Field(strict=True, ge=1, le=MAX_FILE)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("filename")
    @classmethod
    def filename_safe(cls, value):
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 or c in '/\\' for c in value):
            raise ValueError("invalid filename")
        if Path(value).suffix.lower() not in {".pdf", ".pptx"}:
            raise ValueError("unsupported file")
        return value


def material_manifest(row):
    result = {key: row[key] for key in (
        "id", "course_id", "lecture_id", "filename", "kind", "size_bytes", "uploaded_bytes",
        "status", "revision", "created_at", "updated_at",
    )}
    code = row['error_code']
    result.update(error_code=code if code in SAFE_ERRORS else ("conversion_failed" if code else None),
                  error=SAFE_ERRORS.get(code, SAFE_ERRORS['conversion_failed'] if code else None),
                  unit_count=0, warning_count=0)
    if row['status'] == 'ready':
        try:
            document = json.loads(row['document_json'])
            result.update(unit_count=document['unit_count'], warning_count=len(document.get('warnings', [])))
        except (ValueError, TypeError, KeyError):
            result.update(status='failed', error_code='conversion_failed', error=SAFE_ERRORS['conversion_failed'])
    return result


class MaterialDownloadResponse(StreamingResponse):
    """Close an owned descriptor even if sending fails before first iteration."""
    def __init__(self, descriptor, **kwargs):
        self._descriptor = descriptor
        self._descriptor_lock = threading.Lock()
        try:
            super().__init__(self._chunks(), **kwargs)
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._descriptor_lock:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None

    def _chunks(self):
        try:
            while True:
                with self._descriptor_lock:
                    data = os.read(self._descriptor, 64 * 1024) if self._descriptor is not None else b''
                if not data:
                    break
                yield data
        finally:
            self.close()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()


class MaterialService:
    def __init__(self, settings, database, limiter, *, converter=None):
        self.settings, self.database, self.limiter = settings, database, limiter
        self.directory = Path(settings.data_dir) / 'study-materials'
        self.converter = converter
        self.lock = threading.RLock()
        self.capacity = threading.BoundedSemaphore(2)
        self.shutdown = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='material-conversion')
        self.futures = set()
        self._unsettled = {}

    @staticmethod
    def _scope(connection, username, *, course_id=None, lecture_id=None):
        if bool(course_id) == bool(lecture_id):
            raise HTTPException(422, '자료를 연결할 강의 또는 수업을 선택해 주세요.')
        if course_id:
            found = connection.execute('SELECT 1 FROM course_groups WHERE id=? AND username=?', (course_id, username)).fetchone()
        else:
            found = connection.execute('SELECT 1 FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL', (lecture_id, username)).fetchone()
        if found is None:
            raise HTTPException(404, '자료를 찾을 수 없습니다.')

    @staticmethod
    def _access(connection):
        row = connection.execute('SELECT access_enabled FROM operational_state WHERE singleton=1').fetchone()
        if row is None or not row[0]:
            raise HTTPException(503, '현재 수업 서비스가 일시 중지되었습니다.')

    def _owned(self, connection, material_id, username):
        row = connection.execute('SELECT * FROM study_materials WHERE id=? AND username=?', (material_id, username)).fetchone()
        if row is None:
            raise HTTPException(404, '자료를 찾을 수 없습니다.')
        self._scope(connection, username, course_id=row['course_id'], lecture_id=row['lecture_id'])
        return row

    def _path(self, row):
        identifier = str(uuid.UUID(row['id']))
        expected = identifier + '.' + row['kind']
        if row['kind'] not in ('pdf', 'pptx') or row['storage_name'] != expected:
            raise ValueError('unsafe material storage')
        ensure_private_directory(self.directory)
        return self.directory / expected

    def reserve(self, body, username, *, course_id=None, lecture_id=None):
        material_id = str(body.id)
        kind = Path(body.filename).suffix.lower()[1:]
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._access(connection)
            self._scope(connection, username, course_id=course_id, lecture_id=lecture_id)
            existing = connection.execute('SELECT * FROM study_materials WHERE id=?', (material_id,)).fetchone()
            if existing is not None:
                if (existing['username'] != username or any(existing[k] != v for k, v in {
                        'course_id': course_id, 'lecture_id': lecture_id, 'filename': body.filename,
                        'size_bytes': body.size_bytes, 'sha256': body.sha256, 'kind': kind}.items())):
                    raise HTTPException(409, '같은 자료 요청 ID로 다른 파일을 올릴 수 없습니다.')
                return material_manifest(existing)
            scope_key, scope_value = ('course_id', course_id) if course_id else ('lecture_id', lecture_id)
            count = connection.execute('SELECT count(*) FROM study_materials WHERE username=? AND '+scope_key+'=?', (username, scope_value)).fetchone()[0]
            used = connection.execute('SELECT COALESCE(sum(size_bytes),0) FROM study_materials WHERE username=?', (username,)).fetchone()[0]
            if count >= MAX_SCOPE_FILES or used + body.size_bytes > MAX_OWNER_BYTES:
                raise HTTPException(413, '자료 보관 한도에 도달했습니다. 필요 없는 자료를 정리한 뒤 다시 올려 주세요.')
            if not self.limiter.allow(('material-upload', username), 30, 3600):
                raise HTTPException(429, '자료 업로드 요청이 많습니다. 잠시 후 다시 시도하세요.')
            timestamp = now()
            connection.execute(
                "INSERT INTO study_materials(id,username,course_id,lecture_id,filename,kind,size_bytes,sha256,storage_name,status,error_code,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'processing','awaiting_upload',1,?,?)",
                (material_id, username, course_id, lecture_id, body.filename, kind, body.size_bytes, body.sha256,
                 material_id+'.'+kind, timestamp, timestamp))
            return material_manifest(connection.execute('SELECT * FROM study_materials WHERE id=?', (material_id,)).fetchone())

    def write_part(self, material_id, username, content, offset, part_hash):
        if not content or len(content) > MAX_PART or offset < 0 or hashlib.sha256(content).hexdigest() != part_hash:
            raise HTTPException(422, '자료 조각의 크기 또는 확인값이 올바르지 않습니다.')
        with self.lock, self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._owned(connection, material_id, username)
            self._access(connection)
            end = offset + len(content)
            if end > row['size_bytes'] or offset > row['uploaded_bytes']:
                raise HTTPException(409, '자료 전송 위치가 일치하지 않습니다. 저장 상태를 확인해 주세요.')
            if row['status'] != 'processing' or row['error_code'] != 'awaiting_upload':
                # A repeated final chunk is harmless only after byte comparison.
                if end > row['uploaded_bytes']:
                    raise HTTPException(409, '이미 변환을 시작한 자료입니다.')
            path = self._path(row)
            flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if row['uploaded_bytes'] == 0 and not path.exists() else 0)
            fd = open_file(path, flags, private=True)
            try:
                size = os.fstat(fd).st_size
                if size < row['uploaded_bytes']:
                    raise HTTPException(409, '저장된 자료 길이가 달라 다시 업로드해야 합니다.')
                if size > row['uploaded_bytes']:
                    os.ftruncate(fd, row['uploaded_bytes'])
                if offset < row['uploaded_bytes']:
                    if end > row['uploaded_bytes']:
                        raise HTTPException(409, '자료 조각 경계가 일치하지 않습니다.')
                    os.lseek(fd, offset, os.SEEK_SET)
                    if os.read(fd, len(content)) != content:
                        raise HTTPException(409, '이미 받은 위치에 다른 자료를 저장할 수 없습니다.')
                    return material_manifest(row)
                os.lseek(fd, offset, os.SEEK_SET)
                with os.fdopen(fd, 'r+b', closefd=False) as stream:
                    stream.write(content); stream.flush(); os.fsync(fd)
                connection.execute('UPDATE study_materials SET uploaded_bytes=?,updated_at=? WHERE id=?', (end, now(), material_id))
            finally:
                os.close(fd)
            return material_manifest(connection.execute('SELECT * FROM study_materials WHERE id=?', (material_id,)).fetchone())

    def start_conversion(self, material_id, username):
        self._settle_failures()
        acquired = submitted = False
        try:
            with self.lock, self.database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row = self._owned(connection, material_id, username)
                self._access(connection)
                if row['status'] == 'ready' or row['error_code'] == 'converting':
                    return material_manifest(row)
                if self.shutdown.is_set() or not self.capacity.acquire(blocking=False):
                    raise HTTPException(429, '다른 자료를 변환하고 있습니다. 잠시 후 다시 시도하세요.')
                acquired = True
                if row['uploaded_bytes'] != row['size_bytes']:
                    raise HTTPException(409, '자료 전송이 끝난 뒤 변환할 수 있습니다.')
                fd = open_file(self._path(row), os.O_RDONLY, private=True)
                with os.fdopen(fd, 'rb') as stream:
                    size = os.fstat(stream.fileno()).st_size
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest != row['sha256'] or size != row['size_bytes']:
                    raise HTTPException(409, '원본 자료 확인값이 일치하지 않습니다. 다시 업로드해 주세요.')
                connection.execute("UPDATE study_materials SET status='processing',error_code='converting',document_json=NULL,updated_at=? WHERE id=?", (now(), material_id))
                job = dict(connection.execute('SELECT * FROM study_materials WHERE id=?', (material_id,)).fetchone())
            # The slot is retained across commit; every failure before callback
            # ownership releases it, including a context-manager commit error.
            try:
                future = self.executor.submit(self._convert, job)
                with self.lock:
                    self.futures.add(future)
                future.add_done_callback(self._finished)
                submitted = True
            except Exception:
                self._fail(job, 'interrupted')
                raise HTTPException(503, SAFE_ERRORS['interrupted']) from None
            return material_manifest(job)
        finally:
            if acquired and not submitted:
                self.capacity.release()

    def _finished(self, future):
        with self.lock:
            self.futures.discard(future)
        self.capacity.release()

    def _settle_failures(self):
        # Only retry saving a terminal state, never the converter itself.
        with self.lock:
            jobs = list(self._unsettled.values())
        for job, code in jobs:
            self._fail(job, code)

    def _fail(self, job, code):
        try:
            with self.database.connect() as connection:
                connection.execute("UPDATE study_materials SET status='failed',error_code=?,document_json=NULL,updated_at=? WHERE id=? AND username=? AND status='processing' AND error_code='converting'", (code if code in SAFE_ERRORS else 'conversion_failed', now(), job['id'], job['username']))
        except Exception:
            with self.lock:
                self._unsettled[job['id']] = (job, code)
            raise
        else:
            with self.lock:
                self._unsettled.pop(job['id'], None)

    def _convert(self, job):
        try:
            converter = self.converter
            if converter is None:
                from .material_conversion import convert_material
                converter = convert_material
            document = converter(self._path(job), job['kind'], cancel=self.shutdown.is_set)
            if document['sha256'] != job['sha256'] or document['size_bytes'] != job['size_bytes']:
                raise ValueError('changed input')
            encoded = json.dumps(document, ensure_ascii=False, separators=(',', ':'))
            if len(encoded.encode('utf-8')) > 12 * 1024 * 1024:
                raise ValueError('conversion output too large')
            with self.lock, self.database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row = self._owned(connection, job['id'], job['username'])
                self._access(connection)
                if self.shutdown.is_set() or row['error_code'] != 'converting' or row['revision'] != job['revision']:
                    raise ValueError('interrupted')
                connection.execute("UPDATE study_materials SET status='ready',error_code=NULL,document_json=?,revision=revision+1,updated_at=? WHERE id=?", (encoded, now(), job['id']))
        except Exception as error:
            code = getattr(error, 'code', 'conversion_failed')
            code = {'size_limit':'limits_exceeded','archive_limit':'limits_exceeded','unit_limit':'limits_exceeded','output_limit':'limits_exceeded','timeout':'limits_exceeded','dependency_missing':'dependency_unavailable','isolation_unavailable':'dependency_unavailable','unsupported_type':'invalid_file','invalid_document':'invalid_file','encrypted_document':'invalid_file','cancelled':'interrupted'}.get(code, code)
            self._fail(job, 'interrupted' if self.shutdown.is_set() else code)

    def purge_lecture(self, lecture_id, username):
        self._settle_failures()
        # Called only by the existing confirmed permanent-deletion operation.
        # Keep DB rows/quota until file removal succeeds so retries are truthful.
        with self.lock, self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            lecture = connection.execute('SELECT deleting FROM lectures WHERE id=? AND username=?', (lecture_id, username)).fetchone()
            if lecture is None:
                return True
            if not lecture['deleting']:
                return False
            rows = connection.execute('SELECT * FROM study_materials WHERE lecture_id=? AND username=?', (lecture_id, username)).fetchall()
            if any(row['error_code'] == 'converting' for row in rows):
                return False
            for row in rows:
                path = self._path(row)
                if path.exists():
                    validate_private_path(path)
                    path.unlink()
            # A composed review contains copied lecture text. Remove that result
            # as part of deleting its source, while preserving unrelated jobs.
            connection.execute("UPDATE course_review_jobs SET status='failed',document_json=NULL,completed_at=NULL,error_code='source_removed',updated_at=? WHERE username=? AND id IN (SELECT job_id FROM course_review_sources WHERE lecture_id=? AND username=?)", (now(), username, lecture_id, username))
        return True

    def recover(self):
        # An interrupted conversion is never silently replayed after restart.
        with self.database.connect() as connection:
            connection.execute("UPDATE study_materials SET status='failed',error_code='interrupted',updated_at=? WHERE status='processing' AND error_code='converting'", (now(),))

    def request_shutdown(self):
        self.shutdown.set()

    def stop(self, timeout=5):
        self.request_shutdown()
        with self.lock:
            pending = set(self.futures)
        _, unfinished = wait(pending, timeout=max(0, timeout)) if pending else (set(), set())
        self.executor.shutdown(wait=False, cancel_futures=False)
        return not unfinished

    def install(self, app, *, identity):
        def list_scope(username, *, course_id=None, lecture_id=None):
            self._settle_failures()
            with self.database.connect() as connection:
                self._scope(connection, username, course_id=course_id, lecture_id=lecture_id)
                key, value = ('course_id', course_id) if course_id else ('lecture_id', lecture_id)
                rows = connection.execute('SELECT '+MATERIAL_LIST_COLUMNS+' FROM study_materials WHERE username=? AND '+key+'=? ORDER BY created_at,id', (username, value)).fetchall()
                return {'materials': [material_manifest(row) for row in rows]}

        @app.get('/lectures/{lecture_id}/materials')
        def lecture_materials(lecture_id: str, user: dict = Depends(identity)):
            return list_scope(user['username'], lecture_id=lecture_id)

        @app.post('/lectures/{lecture_id}/materials', status_code=201)
        def reserve_lecture(lecture_id: str, body: MaterialBody, user: dict = Depends(identity)):
            return self.reserve(body, user['username'], lecture_id=lecture_id)

        @app.get('/courses/{course_id}/materials')
        def course_materials(course_id: str, user: dict = Depends(identity)):
            return list_scope(user['username'], course_id=course_id)

        @app.post('/courses/{course_id}/materials', status_code=201)
        def reserve_course(course_id: str, body: MaterialBody, user: dict = Depends(identity)):
            return self.reserve(body, user['username'], course_id=course_id)

        @app.get('/study-materials/{material_id}')
        def get_material(material_id: str, user: dict = Depends(identity)):
            self._settle_failures()
            with self.database.connect() as connection:
                row = self._owned(connection, material_id, user['username'])
                return {**material_manifest(row), 'document': json.loads(row['document_json']) if row['status'] == 'ready' else None}

        @app.put('/study-materials/{material_id}/content')
        async def upload(material_id: str, request: Request,
                         x_upload_offset: Annotated[int, Header(ge=0)],
                         x_part_sha256: Annotated[str, Header(pattern=r'^[0-9a-f]{64}$')],
                         user: dict = Depends(identity)):
            # Check ownership before reading; the global body boundary also caps
            # every request, so a document never raises the audio upload limit.
            with self.database.connect() as connection:
                self._owned(connection, material_id, user['username'])
            return self.write_part(material_id, user['username'], await request.body(), x_upload_offset, x_part_sha256)

        @app.post('/study-materials/{material_id}/convert', status_code=202)
        def convert(material_id: str, user: dict = Depends(identity)):
            return self.start_conversion(material_id, user['username'])

        @app.get('/study-materials/{material_id}/markdown')
        def markdown(material_id: str, user: dict = Depends(identity)):
            with self.database.connect() as connection:
                row = self._owned(connection, material_id, user['username'])
                if row['status'] != 'ready':
                    raise HTTPException(409, '자료를 변환한 뒤 Markdown을 내려받을 수 있습니다.')
                document = json.loads(row['document_json'])
                return Response(document['markdown'], media_type='text/markdown', headers={
                    'Content-Disposition': 'attachment; filename="lecture-material.md"', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

        @app.get('/study-materials/{material_id}/original')
        def original(material_id: str, user: dict = Depends(identity)):
            with self.lock, self.database.connect() as connection:
                row = self._owned(connection, material_id, user['username'])
                if row['uploaded_bytes'] != row['size_bytes']:
                    raise HTTPException(409, '원본 자료 전송을 먼저 마쳐 주세요.')
                fd = open_file(self._path(row), os.O_RDONLY, private=True)
                if os.fstat(fd).st_size != row['size_bytes']:
                    os.close(fd)
                    raise HTTPException(409, '원본 자료 길이를 확인하지 못했습니다.')
                kind, size = row['kind'], row['size_bytes']
            return MaterialDownloadResponse(fd, media_type='application/octet-stream', headers={
                'Content-Disposition': f'attachment; filename="lecture-material.{kind}"',
                'Content-Length': str(size), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

        @app.delete('/study-materials/{material_id}')
        def delete_material(material_id: str, user: dict = Depends(identity)):
            self._settle_failures()
            with self.lock, self.database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row = self._owned(connection, material_id, user['username'])
                self._access(connection)
                if row['error_code'] == 'converting':
                    raise HTTPException(409, '자료 변환이 끝난 뒤 삭제해 주세요.')
                path = self._path(row)
                if path.exists():
                    validate_private_path(path)
                    path.unlink()
                connection.execute('DELETE FROM study_materials WHERE id=? AND username=?', (material_id, user['username']))
            return {'status': 'deleted'}


def material_snapshot(connection, username, lecture_ids, *, course_id=None):
    """Freeze all attached material units; never truncate or silently skip files.

    Call inside the same transaction as transcript/membership snapshots. Empty
    image pages become explicit unavailable-text evidence, never invented OCR.
    """
    ids = sorted(set(lecture_ids))
    if not ids or len(ids) > 200:
        raise HTTPException(413, '한 번에 정리할 수업 범위를 나누어 주세요.')
    scopes = set()
    for identifier in ids:
        MaterialService._scope(connection, username, lecture_id=identifier)
        scopes.add(('lecture_id', identifier))
        group = connection.execute('SELECT course_id FROM course_sessions WHERE lecture_id=? AND username=?', (identifier, username)).fetchone()
        if group and group['course_id']:
            scopes.add(('course_id', group['course_id']))
    if course_id:
        MaterialService._scope(connection, username, course_id=course_id)
        scopes.add(('course_id', course_id))
    manifest, sources = [], []
    total_encoded = total_chars = 0
    for key, identifier in sorted(scopes):
        descriptors = connection.execute('SELECT id,status,length(CAST(document_json AS BLOB)) AS encoded_size FROM study_materials WHERE username=? AND '+key+'=? ORDER BY id', (username, identifier)).fetchall()
        for descriptor in descriptors:
            if descriptor['status'] != 'ready':
                raise HTTPException(409, '연결된 강의자료의 업로드·변환을 마친 뒤 정리본을 만들어 주세요. 사용하지 않을 자료는 삭제할 수 있습니다.')
            size = descriptor['encoded_size']
            if not isinstance(size, int) or size > 8 * 1024 * 1024 - total_encoded:
                raise HTTPException(413, '연결한 강의자료가 한 번에 참고할 수 있는 크기를 넘었습니다. 자료 범위를 나누어 주세요.')
            total_encoded += size
            row = connection.execute('SELECT * FROM study_materials WHERE id=? AND username=?', (descriptor['id'], username)).fetchone()
            document = json.loads(row['document_json'])
            if not isinstance(document.get('units'), list) or len(document['units']) > 128 - len(sources):
                raise HTTPException(413, '정리본의 보조자료는 한 번에 128페이지·슬라이드까지 참고할 수 있습니다. 자료 범위를 나누어 주세요.')
            manifest.append({'id': row['id'], 'revision': row['revision'], 'sha256': row['sha256'],
                             'document_hash': hashlib.sha256(row['document_json'].encode()).hexdigest(),
                             'course_id': row['course_id'], 'lecture_id': row['lecture_id']})
            for unit in document['units']:
                index = unit['index']
                warnings = unit.get('warnings', [])
                text = unit.get('markdown') or unit.get('text') or '[추출한 글자가 없습니다. 원본 자료를 확인해야 합니다.]'
                if warnings:
                    text += '\n\n[자료 추출 주의: 그림·도표·스캔 등 일부 내용은 텍스트로 확인하지 못할 수 있습니다. 원본을 확인하세요.]'
                total_chars += len(text)
                if len(text) > 24000 or total_chars > 200000:
                    raise HTTPException(413, '정리본 보조자료의 글자 수 제한을 넘었습니다. 자료를 나누어 주세요. 내용을 임의로 생략하지 않았습니다.')
                sources.append({'id': row['id']+':'+str(index), 'label': row['filename'],
                                'kind': row['kind'], 'index': index, 'text': text})
    revision = hashlib.sha256(json.dumps({'scopes': sorted(scopes), 'materials': manifest}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'revision': revision, 'manifest': manifest, 'sources': sources}
