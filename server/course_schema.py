"""Additive schema for explicitly grouped courses, materials and review jobs."""

def migrate_course_schema(connection):
    # Preserve every legacy row. Source code is run only by normal Database
    # initialization, in the same private database chosen by existing settings.
    connection.executescript('''
        CREATE UNIQUE INDEX IF NOT EXISTS lectures_owner_key ON lectures(id,username);
        CREATE TABLE IF NOT EXISTS course_groups (
            id TEXT PRIMARY KEY CHECK(length(id)=36),
            username TEXT NOT NULL REFERENCES users(username),
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
            semester TEXT NOT NULL DEFAULT '' CHECK(length(semester)<=40),
            normalized_name TEXT NOT NULL CHECK(length(normalized_name) BETWEEN 1 AND 4096),
            normalized_semester TEXT NOT NULL DEFAULT '' CHECK(length(normalized_semester)<=4096),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision BETWEEN 1 AND 2147483647),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(id,username), UNIQUE(username,normalized_name,normalized_semester)
        );
        CREATE TABLE IF NOT EXISTS course_sessions (
            lecture_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            course_id TEXT,
            session_name TEXT NOT NULL DEFAULT '' CHECK(length(session_name)<=120),
            session_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision BETWEEN 1 AND 2147483647),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(lecture_id,username) REFERENCES lectures(id,username) ON DELETE CASCADE,
            FOREIGN KEY(course_id,username) REFERENCES course_groups(id,username)
        );
        CREATE INDEX IF NOT EXISTS course_session_members ON course_sessions(username,course_id,session_at,lecture_id);
        CREATE TABLE IF NOT EXISTS study_materials (
            id TEXT PRIMARY KEY CHECK(length(id)=36), username TEXT NOT NULL REFERENCES users(username),
            course_id TEXT, lecture_id TEXT,
            filename TEXT NOT NULL CHECK(length(filename) BETWEEN 1 AND 180),
            kind TEXT NOT NULL CHECK(kind IN ('pdf','pptx')),
            size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 33554432),
            uploaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK(uploaded_bytes BETWEEN 0 AND size_bytes),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256=lower(sha256)),
            storage_name TEXT NOT NULL UNIQUE CHECK(length(storage_name) BETWEEN 1 AND 80),
            status TEXT NOT NULL CHECK(status IN ('processing','ready','failed')),
            document_json TEXT, error_code TEXT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision BETWEEN 1 AND 2147483647),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK((course_id IS NULL) != (lecture_id IS NULL)),
            CHECK((status='ready' AND document_json IS NOT NULL AND error_code IS NULL)
               OR (status!='ready' AND document_json IS NULL)),
            UNIQUE(id,username),
            FOREIGN KEY(course_id,username) REFERENCES course_groups(id,username),
            FOREIGN KEY(lecture_id,username) REFERENCES lectures(id,username) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS materials_owner_scope ON study_materials(username,course_id,lecture_id);
        CREATE TABLE IF NOT EXISTS course_review_jobs (
            id TEXT PRIMARY KEY CHECK(length(id)=36), username TEXT NOT NULL,
            course_id TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','completed','failed')),
            source_revision TEXT NOT NULL CHECK(length(source_revision)=64),
            source_manifest_json TEXT NOT NULL,
            document_json TEXT, error_code TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 1),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
            UNIQUE(id,username),
            FOREIGN KEY(course_id,username) REFERENCES course_groups(id,username),
            CHECK((status='completed' AND document_json IS NOT NULL AND completed_at IS NOT NULL AND error_code IS NULL)
               OR (status!='completed' AND document_json IS NULL AND completed_at IS NULL))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS course_review_active_owner ON course_review_jobs(username)
            WHERE status IN ('queued','processing');
        CREATE TABLE IF NOT EXISTS course_review_sources (
            job_id TEXT NOT NULL, username TEXT NOT NULL, lecture_id TEXT NOT NULL,
            PRIMARY KEY(job_id,lecture_id),
            FOREIGN KEY(job_id,username) REFERENCES course_review_jobs(id,username) ON DELETE CASCADE,
            FOREIGN KEY(lecture_id,username) REFERENCES lectures(id,username) ON DELETE CASCADE
        );
    ''')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(lecture_study_notes)')}
    for name, declaration in (
        ('material_revision', "TEXT NOT NULL DEFAULT ''"),
        ('source_manifest_json', 'TEXT'),
        ('format_version', 'INTEGER NOT NULL DEFAULT 1 CHECK(format_version IN (1,2))'),
    ):
        if name not in columns:
            connection.execute('ALTER TABLE lecture_study_notes ADD COLUMN '+name+' '+declaration)
