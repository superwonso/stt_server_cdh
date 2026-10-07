"""Owner-scoped unresolved ASR output; never a committed transcript or ACK."""
from fastapi import HTTPException
from .model_protocol import AlignmentUnavailableError

MESSAGE = "이 구간의 받아쓰기를 확정하지 못했습니다. 임시 텍스트와 원본 음성을 보관하고, 확인 후 다시 시도해 주세요."


class AlignmentChunkError(HTTPException):
    def __init__(self, issue):
        super().__init__(422, MESSAGE)
        self.issue = public_issue(issue)

    def response(self):
        return {"detail": MESSAGE, "code": "alignment_unavailable", "retryable": False,
                **self.issue}


def migrate(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS transcription_issues (
        lecture_id TEXT PRIMARY KEY REFERENCES lectures(id) ON DELETE CASCADE,
        chunk_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        start_seconds REAL NOT NULL CHECK(start_seconds>=0),
        overlap_seconds REAL NOT NULL CHECK(overlap_seconds>=0),
        duration_seconds REAL NOT NULL CHECK(duration_seconds>0 AND duration_seconds<=15),
        final_chunk INTEGER NOT NULL CHECK(final_chunk IN (0,1)),
        kind TEXT NOT NULL CHECK(kind IN ('chunk','finalize')),
        partial_text TEXT NOT NULL CHECK(length(partial_text) BETWEEN 1 AND 8192
            AND length(CAST(partial_text AS BLOB))<=32768),
        updated_at TEXT NOT NULL
    )""")
    columns = {row[1] for row in connection.execute('PRAGMA table_info(imports)')}
    for name in ('alignment_held', 'alignment_retry_pending'):
        if name not in columns:
            connection.execute(f'ALTER TABLE imports ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0 CHECK({name} IN (0,1))')


def find(connection, lecture_id, username):
    row = connection.execute(
        'SELECT i.* FROM transcription_issues i JOIN lectures l ON l.id=i.lecture_id '
        'WHERE i.lecture_id=? AND l.username=? AND l.deleting=0 AND l.trashed_at IS NULL',
        (lecture_id, username)).fetchone()
    return dict(row) if row is not None else None


def public_issue(issue):
    # Revalidate persisted text before exposing it; corrupt storage is not a
    # reason to invent a successful transcript or reflect arbitrary metadata.
    partial = AlignmentUnavailableError(issue['partial_text']).partial_text
    return {'chunk_id': issue['chunk_id'], 'start_seconds': issue['start_seconds'],
            'final_chunk': bool(issue['final_chunk']), 'kind': issue['kind'],
            'partial_text': partial, 'code': 'alignment_unavailable'}


def public_issues(connection, lecture_id, username):
    issue = find(connection, lecture_id, username)
    return [public_issue(issue)] if issue is not None else []


def check(connection, lecture, chunk_id, payload_hash, start, overlap, final,
          *, kind='chunk', allow_retry=False):
    issue = find(connection, lecture['id'], lecture['username'])
    if issue is None:
        return
    if issue['chunk_id'] != chunk_id or issue['kind'] != kind:
        raise HTTPException(409, '이 수업에 확인이 필요한 음성 구간이 있습니다. 해당 구간을 먼저 확인해 주세요.')
    if (issue['payload_hash'], issue['start_seconds'], issue['overlap_seconds'], bool(issue['final_chunk'])) != (
            payload_hash, start, overlap, bool(final)):
        raise HTTPException(409, '같은 음성 ID의 원본 또는 시간 정보가 달라졌습니다.')
    if not allow_retry:
        raise AlignmentChunkError(issue)


def save(connection, lecture, chunk_id, payload_hash, start, overlap, duration,
         final, partial_text, updated_at, *, kind='chunk'):
    partial = AlignmentUnavailableError(partial_text).partial_text
    current = connection.execute(
        'SELECT recording_finalized FROM lectures WHERE id=? AND username=? '
        'AND deleting=0 AND trashed_at IS NULL', (lecture['id'], lecture['username'])).fetchone()
    if current is None:
        raise HTTPException(404, '수업을 찾을 수 없습니다.')
    if current['recording_finalized']:
        raise HTTPException(409, '이미 종료된 수업의 처리 상태가 바뀌었습니다.')
    check(connection, lecture, chunk_id, payload_hash, start, overlap, final,
          kind=kind, allow_retry=True)
    connection.execute('INSERT INTO transcription_issues '
        '(lecture_id,chunk_id,payload_hash,start_seconds,overlap_seconds,duration_seconds,final_chunk,kind,partial_text,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(lecture_id) DO UPDATE SET '
        'partial_text=excluded.partial_text,updated_at=excluded.updated_at',
        (lecture['id'],chunk_id,payload_hash,start,overlap,duration,int(final),kind,partial,updated_at))
    # Consume the explicit import retry in the same transaction as its failed
    # receipt. A crash before the worker marks the job held must not leave a
    # persisted grant that bypasses this cached failure on the next startup.
    connection.execute('UPDATE imports SET alignment_retry_pending=0 '
        'WHERE lecture_id=? AND username=? AND alignment_retry_pending=1',
        (lecture['id'], lecture['username']))
    return find(connection, lecture['id'], lecture['username'])


def clear(connection, lecture_id, chunk_id):
    connection.execute('DELETE FROM transcription_issues WHERE lecture_id=? AND chunk_id=?', (lecture_id, chunk_id))
