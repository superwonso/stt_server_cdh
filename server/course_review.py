"""Explicit owner-scoped courses and session metadata; no inferred merges."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .material_service import MATERIAL_LIST_COLUMNS, material_manifest, material_snapshot


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, maximum, empty=True):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('invalid course text')
    value=value.strip()
    if not empty and not value:
        raise ValueError('empty course name')
    return value


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


class CourseCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: uuid.UUID
    name: str
    semester: str = ''

    @field_validator('name','semester',mode='before')
    @classmethod
    def check_text(cls, value, info):
        return _text(value,80 if info.field_name=='name' else 40,info.field_name!='name')


class CoursePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(strict=True,ge=1,le=2147483646)
    name: str | None = None
    semester: str | None = None

    @field_validator('name','semester',mode='before')
    @classmethod
    def check_text(cls,value,info):
        return _text(value,80 if info.field_name=='name' else 40,info.field_name!='name')


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(strict=True,ge=0,le=2147483646)
    course_id: uuid.UUID | None = None
    session_name: str = ''
    session_at: str | None = None

    @field_validator('session_name',mode='before')
    @classmethod
    def check_text(cls,value):
        return _text(value,120)

    @field_validator('session_at')
    @classmethod
    def check_time(cls,value):
        if value is None:
            return value
        if len(value)>40:
            raise ValueError('invalid session time')
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
        if value.tzinfo is None or not 1900<=value.year<=2200:
            raise ValueError('session timezone required')
        return value.astimezone(timezone.utc).isoformat()


def _course(connection,course_id,username):
    row=connection.execute('SELECT * FROM course_groups WHERE id=? AND username=?',(course_id,username)).fetchone()
    if row is None:
        raise HTTPException(404,'강의를 찾을 수 없습니다.')
    return row


def _lecture(connection,lecture_id,username):
    row=connection.execute('SELECT * FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL',(lecture_id,username)).fetchone()
    if row is None:
        raise HTTPException(404,'수업을 찾을 수 없습니다.')
    return row


def course_result(row):
    return {key:row[key] for key in ('id','name','semester','revision','created_at','updated_at')}


def course_session_for(connection,lecture):
    row=connection.execute('SELECT s.*,c.name AS course_name FROM course_sessions s LEFT JOIN course_groups c ON c.id=s.course_id AND c.username=s.username WHERE s.lecture_id=? AND s.username=?',(lecture['id'],lecture['username'])).fetchone()
    return {'course_id':row['course_id'] if row else None,'course_name':row['course_name'] if row and row['course_name'] else '',
            'session_name':row['session_name'] if row else '', 'session_at':row['session_at'] if row else None,
            'session_revision':row['revision'] if row else 0}


def active_lecture_review(connection,lecture_id):
    return connection.execute("SELECT 1 FROM course_review_sources s JOIN course_review_jobs j ON j.id=s.job_id AND j.username=s.username WHERE s.lecture_id=? AND j.status IN ('queued','processing') LIMIT 1",(lecture_id,)).fetchone() is not None


def install_courses(app,database,*,identity,limiter):
    def allow(username):
        if not limiter.allow(('course-write',username),60,60):
            raise HTTPException(429,'강의 정보 변경이 많습니다. 잠시 후 다시 시도하세요.')

    @app.get('/courses')
    def list_courses(offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200),user:dict=Depends(identity)):
        with database.connect() as connection:
            total=connection.execute('SELECT count(*) FROM course_groups WHERE username=?',(user['username'],)).fetchone()[0]
            rows=connection.execute('SELECT * FROM course_groups WHERE username=? ORDER BY name,semester,id LIMIT ? OFFSET ?',(user['username'],limit,offset)).fetchall()
            courses=[]
            for row in rows:
                count=connection.execute('SELECT count(*) FROM course_sessions s JOIN lectures l ON l.id=s.lecture_id AND l.username=s.username WHERE s.course_id=? AND s.username=? AND l.deleting=0 AND l.trashed_at IS NULL',(row['id'],user['username'])).fetchone()[0]
                courses.append({**course_result(row),'session_count':count})
        return {'courses':courses,'total':total,'offset':offset,'next_offset':offset+len(rows) if offset+len(rows)<total else None}

    @app.post('/courses',status_code=201)
    def create_course(body:CourseCreate,user:dict=Depends(identity)):
        allow(user['username'])
        identifier=str(body.id)
        try:
            with database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row=connection.execute('SELECT * FROM course_groups WHERE id=?',(identifier,)).fetchone()
                if row:
                    if row['username']!=user['username'] or row['name']!=body.name or row['semester']!=body.semester:
                        raise HTTPException(409,'같은 요청 ID로 다른 강의를 만들 수 없습니다.')
                    return course_result(row)
                if connection.execute('SELECT count(*) FROM course_groups WHERE username=?',(user['username'],)).fetchone()[0]>=500:
                    raise HTTPException(413,'등록할 수 있는 강의 수를 넘었습니다.')
                timestamp=_now()
                connection.execute('INSERT INTO course_groups(id,username,name,semester,normalized_name,normalized_semester,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(identifier,user['username'],body.name,body.semester,normalized(body.name),normalized(body.semester),timestamp,timestamp))
                return course_result(_course(connection,identifier,user['username']))
        except sqlite3.IntegrityError:
            raise HTTPException(409,'같은 이름과 학기의 강의가 이미 있습니다. 기존 강의를 선택해 주세요.') from None

    @app.get('/courses/{course_id}')
    def get_course(course_id:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200),user:dict=Depends(identity)):
        with database.connect() as connection:
            course=_course(connection,course_id,user['username'])
            params=(course_id,user['username'])
            query=' FROM course_sessions s JOIN lectures l ON l.id=s.lecture_id AND l.username=s.username WHERE s.course_id=? AND s.username=? AND l.deleting=0 AND l.trashed_at IS NULL'
            total=connection.execute('SELECT count(*)'+query,params).fetchone()[0]
            rows=connection.execute('SELECT l.*'+query+' ORDER BY COALESCE(s.session_at,l.created_at),l.id LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
            sessions=[]
            for row in rows:
                note=connection.execute("SELECT status FROM lecture_study_notes WHERE lecture_id=? AND username=?",(row['id'],user['username'])).fetchone()
                sessions.append({'id':row['id'],'title':row['title'],'created_at':row['created_at'],
                                 'recording_finalized':bool(row['recording_finalized']),
                                 'study_note_status':note['status'] if note else None,
                                 **course_session_for(connection,row)})
            materials=connection.execute('SELECT '+MATERIAL_LIST_COLUMNS+' FROM study_materials WHERE username=? AND (course_id=? OR lecture_id IN (SELECT s.lecture_id FROM course_sessions s JOIN lectures l ON l.id=s.lecture_id AND l.username=s.username WHERE s.username=? AND s.course_id=? AND l.deleting=0 AND l.trashed_at IS NULL)) ORDER BY created_at,id',(user['username'],course_id,user['username'],course_id)).fetchall()
            return {'course':course_result(course),'sessions':sessions,'total':total,'offset':offset,
                    'next_offset':offset+len(rows) if offset+len(rows)<total else None,'materials':[material_manifest(row) for row in materials]}

    @app.patch('/courses/{course_id}')
    def patch_course(course_id:str,body:CoursePatch,user:dict=Depends(identity)):
        allow(user['username'])
        try:
            with database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row=_course(connection,course_id,user['username'])
                if row['revision']!=body.revision:
                    raise HTTPException(409,'다른 화면에서 강의 정보가 바뀌었습니다. 다시 불러와 주세요.')
                values={**dict(row),**body.model_dump(exclude_unset=True)}
                connection.execute('UPDATE course_groups SET name=?,semester=?,normalized_name=?,normalized_semester=?,revision=revision+1,updated_at=? WHERE id=? AND username=?',(values['name'],values['semester'],normalized(values['name']),normalized(values['semester']),_now(),course_id,user['username']))
                return course_result(_course(connection,course_id,user['username']))
        except sqlite3.IntegrityError:
            raise HTTPException(409,'같은 이름과 학기의 강의가 이미 있습니다. 수업별 강의 배정에서 명시적으로 묶어 주세요.') from None

    @app.get('/lectures/{lecture_id}/course-session')
    def get_session(lecture_id:str,user:dict=Depends(identity)):
        with database.connect() as connection:
            lecture=_lecture(connection,lecture_id,user['username'])
            return {'lecture_id':lecture_id,**course_session_for(connection,lecture)}

    @app.put('/lectures/{lecture_id}/course-session')
    def set_session(lecture_id:str,body:SessionPatch,user:dict=Depends(identity)):
        allow(user['username'])
        with database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            lecture=_lecture(connection,lecture_id,user['username'])
            current=course_session_for(connection,lecture)
            if current['session_revision']!=body.revision:
                raise HTTPException(409,'다른 화면에서 수업 정보가 바뀌었습니다. 다시 불러와 주세요.')
            course_id=str(body.course_id) if body.course_id else None
            if 'course_id' not in body.model_fields_set:
                course_id=current['course_id']
            if course_id:
                _course(connection,course_id,user['username'])
            name=body.session_name if 'session_name' in body.model_fields_set else current['session_name']
            date=body.session_at if 'session_at' in body.model_fields_set else current['session_at']
            connection.execute('INSERT INTO course_sessions(lecture_id,username,course_id,session_name,session_at,revision,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(lecture_id) DO UPDATE SET course_id=excluded.course_id,session_name=excluded.session_name,session_at=excluded.session_at,revision=excluded.revision,updated_at=excluded.updated_at',(lecture_id,user['username'],course_id,name,date,current['session_revision']+1,_now()))
            return {'lecture_id':lecture_id,**course_session_for(connection,lecture)}

class ReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: uuid.UUID
    lecture_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=200)


class CourseReviewService:
    """One explicit user request creates a full, ordered review bundle.

    Each session is processed with its entire source. No session is summarized
    away to fit another session's context window. Started provider work is not
    replayed after interruption. Completed source notes remain separate.
    """
    def __init__(self, settings, database, engine, limiter):
        self.settings,self.database,self.engine,self.limiter=settings,database,engine,limiter
        self.shutdown,self.wake=threading.Event(),threading.Event()
        self.lock,self.process_lock=threading.Lock(),threading.Lock()
        self.thread=None
        self._unsettled=None

    @property
    def model(self):
        return getattr(self.engine,'model',self.settings.translation_model)

    @property
    def configured(self):
        return bool(getattr(self.engine,'configured',False))

    def snapshot(self,connection,username,course_id,selected=None):
        course=_course(connection,course_id,username)
        rows=connection.execute('SELECT l.* FROM course_sessions s JOIN lectures l ON l.id=s.lecture_id AND l.username=s.username WHERE s.course_id=? AND s.username=? AND l.deleting=0 AND l.trashed_at IS NULL ORDER BY COALESCE(s.session_at,l.created_at),l.id',(course_id,username)).fetchall()
        if selected is not None:
            identifiers=set(selected)
            if len(identifiers)!=len(selected):
                raise HTTPException(422,'같은 수업을 중복해서 선택할 수 없습니다.')
            rows=[row for row in rows if row['id'] in identifiers]
            if {row['id'] for row in rows}!=identifiers:
                raise HTTPException(404,'선택한 강의의 수업을 찾을 수 없습니다.')
        if not rows:
            raise HTTPException(409,'강의에 수업을 먼저 연결해 주세요.')
        if len(rows)>200:
            raise HTTPException(413,'한 번에 복습할 수업은 200개까지 선택할 수 있습니다. 나머지 수업도 별도로 선택할 수 있습니다.')
        from .study_notes import StudyNoteError, validate_study_note_source, validate_supporting_sources
        manifest=[]
        total_chars=0
        for row in rows:
            if not row['recording_finalized']:
                raise HTTPException(409,'선택 범위에 녹음 또는 저장이 끝나지 않은 수업이 있습니다. 모든 수업을 마치거나 완료한 수업을 직접 선택해 주세요.')
            raw=self.raw_segments(connection,row['id'])
            try:
                validate_study_note_source(raw)
            except StudyNoteError as error:
                raise HTTPException(413 if error.code=="source_too_large" else 422,"정리할 원문이 없거나 처리할 수 있는 크기를 넘었습니다. 수업 범위를 확인해 주세요.") from None
            total_chars+=sum(len(segment['text']) for segment in raw)
            if total_chars>4_000_000:
                raise HTTPException(413,'복습 자료가 한 번에 처리할 수 있는 크기를 넘었습니다. 수업 범위를 나누어 주세요. 내용을 임의로 생략하지 않았습니다.')
            materials=material_snapshot(connection,username,[row['id']],course_id=course_id)
            try:
                validate_supporting_sources(materials['sources'])
            except StudyNoteError:
                raise HTTPException(413,"강의자료가 한 번에 처리할 수 있는 크기를 넘었습니다. 자료 범위를 나누어 주세요.") from None
            meta=course_session_for(connection,row)
            manifest.append({'lecture_id':row['id'],'raw_revision':self.revision(raw),
                             'material_revision':materials['revision'],'metadata':meta,
                             'title':row['title'],'created_at':row['created_at'],'language':row['language']})
        snapshot={'course':course_result(course),'all_sessions':selected is None,'sessions':manifest}
        revision=hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return snapshot,revision

    def _snapshot_for_job(self,connection,job):
        original=json.loads(job['source_manifest_json'])
        selected=None if original['all_sessions'] else [item['lecture_id'] for item in original['sessions']]
        return self.snapshot(connection,job['username'],job['course_id'],selected)

    def _fail(self,connection,job,code):
        connection.execute("UPDATE course_review_jobs SET status='failed',document_json=NULL,completed_at=NULL,error_code=?,updated_at=? WHERE id=? AND username=? AND status IN ('queued','processing')",(code,_now(),job['id'],job['username']))

    def _interrupted(self,job):
        if self.shutdown.is_set():
            return True
        with self.database.connect() as connection:
            from .material_service import MaterialService
            try:
                MaterialService._access(connection)
                row=connection.execute("SELECT status FROM course_review_jobs WHERE id=? AND username=?",(job['id'],job['username'])).fetchone()
                if not row or row['status']!='processing':
                    return True
                # Ownership/visibility and membership are cheap to recheck at
                # each model boundary; full hash checks occur before/after work.
                snapshot=json.loads(job['source_manifest_json'])
                for item in snapshot['sessions']:
                    lecture=_lecture(connection,item['lecture_id'],job['username'])
                    if not lecture['recording_finalized'] or course_session_for(connection,lecture)['course_id']!=job['course_id']:
                        return True
                return False
            except HTTPException:
                return True

    def result(self,connection,row):
        result={key:row[key] for key in ('id','course_id','status','model','created_at','updated_at','completed_at')}
        result.update(document=None,markdown=None,stale=False,error_code=row['error_code'])
        snapshot=json.loads(row['source_manifest_json'])
        result['session_count']=len(snapshot['sessions'])
        if row['status']!='completed':
            return result
        from .study_notes import StudyNoteError
        try:
            try:
                _,current_revision=self._snapshot_for_job(connection,row)
                result['stale']=current_revision!=row['source_revision']
            except (HTTPException,StudyNoteError,ValueError):
                # New unfinished sessions/materials mark a saved review stale;
                # the original source visibility is checked independently below.
                result['stale']=True
            document=json.loads(row['document_json'])
            if (document.get('format')!='course_review' or document.get('version')!=1
                    or len(document.get('sessions',[]))!=len(snapshot['sessions'])):
                raise ValueError('invalid bundle')
            from .study_notes import validate_unified_study_note_document, study_note_markdown
            overview=[]
            texts=['# 강의 복습 정리본','','핵심 흐름과 수업별 상세 정리를 함께 보관합니다. 원문은 각 수업 정리본에 모두 포함됩니다.','']
            for item,source in zip(document['sessions'],snapshot['sessions'],strict=True):
                if item['lecture_id']!=source['lecture_id']:
                    raise ValueError('changed bundle')
                _lecture(connection,item['lecture_id'],row['username'])
                raw=self.raw_segments(connection,item['lecture_id'])
                note=validate_unified_study_note_document(item['document'],raw)
                overview.extend({'lecture_id':item['lecture_id'],'session_name':source['metadata']['session_name'] or source['title'],**point} for point in note['overview'])
                texts.extend(['## '+source['metadata']['session_name'].replace('#','') if source['metadata']['session_name'] else '## 수업',
                              '', '- 수업 일시: '+(source['metadata']['session_at'] or '미지정'),'',study_note_markdown(note,raw),''])
            document['overview']=overview
            if overview:
                def plain(value):
                    return re.sub(r'([\\`*_{}\[\]()<>#+.!|:~=\-])',r'\\\1',value)
                summary=['## 전체 수업 핵심 흐름','']
                summary.extend('- '+plain(point['session_name'])+' — '+plain(point['text']) for point in overview)
                texts[4:4]=summary+['']
            result.update(document=document,markdown='\n'.join(texts))
        except (HTTPException,StudyNoteError,ValueError,TypeError,KeyError):
            result.update(status='failed',stale=True,error_code='source_changed')
        return result

    def process_next(self):
        if not self.process_lock.acquire(blocking=False):
            return False
        try:
            return self._process_next()
        finally:
            self.process_lock.release()

    def _process_next(self):
        if self._unsettled is not None:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._fail(connection,self._unsettled,"save_failed")
            self._unsettled=None
        if self.shutdown.is_set() or not self.configured:
            return False
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            from .material_service import MaterialService
            try:
                MaterialService._access(connection)
            except HTTPException:
                return False
            row=connection.execute("SELECT * FROM course_review_jobs WHERE status='queued' AND attempts=0 ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return False
            job=dict(row)
            try:
                snapshot,revision=self._snapshot_for_job(connection,job)
                if revision!=job['source_revision'] or job['model']!=self.model:
                    raise ValueError('source changed')
            except Exception:
                self._fail(connection,job,'source_changed')
                return True
            connection.execute("UPDATE course_review_jobs SET status='processing',attempts=1,updated_at=? WHERE id=?",(_now(),job['id']))
            self._unsettled=job
        document={'format':'course_review','version':1,'course':snapshot['course'],'sessions':[],'warnings':[]}
        failure=None
        from .study_notes import coerce_unified_study_note_document
        try:
            for item in snapshot['sessions']:
                if self._interrupted(job):
                    raise RuntimeError('interrupted')
                with self.database.connect() as connection:
                    raw=self.raw_segments(connection,item['lecture_id'])
                    materials=material_snapshot(connection,job['username'],[item['lecture_id']],course_id=job['course_id'])
                    if self.revision(raw)!=item['raw_revision'] or materials['revision']!=item['material_revision']:
                        raise RuntimeError('source changed')
                arguments={'language':item['language'],'segments':copy.deepcopy(raw),'interrupted':lambda:self._interrupted(job)}
                try:
                    if hasattr(self.engine,'create_unified'):
                        output=self.engine.create_unified(**arguments,supporting_sources=copy.deepcopy(materials['sources']))
                    else:
                        output=self.engine.create(**arguments)
                    note=coerce_unified_study_note_document(output.to_dict(),raw,supporting_sources=materials['sources'])
                except Exception:
                    if self._interrupted(job):
                        raise
                    # An unavailable later request does not remove a lecture
                    # from the bundle; preserve the entire original as fallback.
                    note=coerce_unified_study_note_document({'format':'draft','text':'AI 정리 결과를 확인하지 못해 원문을 보존합니다.','warnings':['gateway_unavailable']},raw,supporting_sources=materials['sources'])
                    document['warnings'].append('incomplete_batches')
                document['sessions'].append({'lecture_id':item['lecture_id'],'session_name':item['metadata']['session_name'],
                                             'session_at':item['metadata']['session_at'],'created_at':item['created_at'],
                                             'document':note})
            encoded=json.dumps(document,ensure_ascii=False,allow_nan=False)
            if len(encoded.encode())>64*1024*1024:
                raise ValueError('review output limit')
        except Exception:
            failure='interrupted' if self.shutdown.is_set() else 'review_failed'
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                MaterialService._access(connection)
                _,revision=self._snapshot_for_job(connection,job)
                valid=revision==job['source_revision'] and not self.shutdown.is_set()
            except Exception:
                valid=False
            if not valid:
                self._fail(connection,job,'source_changed')
            elif failure:
                self._fail(connection,job,failure)
            else:
                timestamp=_now()
                connection.execute("UPDATE course_review_jobs SET status='completed',document_json=?,error_code=NULL,updated_at=?,completed_at=? WHERE id=? AND username=? AND status='processing'",(encoded,timestamp,timestamp,job['id'],job['username']))
        self._unsettled=None
        return True

    def recover(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE course_review_jobs SET status='failed',error_code='interrupted',updated_at=? WHERE status='processing' OR (status='queued' AND attempts>0)",(_now(),))
            if not self.configured:
                connection.execute("UPDATE course_review_jobs SET status='failed',error_code='not_configured',updated_at=? WHERE status='queued'",(_now(),))

    def start(self):
        with self.lock:
            if self.shutdown.is_set() or not self.configured or self.thread and self.thread.is_alive():
                return
            self.thread=threading.Thread(target=self._run,name='course-review',daemon=True)
            self.thread.start()

    def _run(self):
        while not self.shutdown.is_set():
            try:
                if self.process_next():
                    continue
            except Exception:
                # No automatic provider replay: any claimed row remains claimed
                # and recovery terminates it after an uncertain persistence error.
                pass
            self.wake.wait(1);self.wake.clear()

    def request_shutdown(self):
        self.shutdown.set();self.wake.set()

    def stop(self,timeout=5):
        self.request_shutdown()
        if self.thread and self.thread.is_alive():
            self.thread.join(max(0,timeout))
        stopped=not self.thread or not self.thread.is_alive()
        if stopped and hasattr(self.engine,'close'):
            self.engine.close()
        return stopped

    def install(self,app,*,identity,raw_segments,transcript_revision):
        self.raw_segments,self.revision=raw_segments,transcript_revision

        @app.post('/courses/{course_id}/reviews',status_code=202)
        def create_review(course_id:str,body:ReviewBody,user:dict=Depends(identity)):
            identifier=str(body.id)
            with self.database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                from .material_service import MaterialService
                MaterialService._access(connection)
                _course(connection,course_id,user['username'])
                previous=connection.execute('SELECT * FROM course_review_jobs WHERE id=?',(identifier,)).fetchone()
                selected=[str(value) for value in body.lecture_ids] if body.lecture_ids is not None else None
                if previous:
                    old=json.loads(previous['source_manifest_json'])
                    old_ids=None if old['all_sessions'] else [item['lecture_id'] for item in old['sessions']]
                    if (previous['username']!=user['username'] or previous['course_id']!=course_id
                            or (None if selected is None else sorted(selected))!=(None if old_ids is None else sorted(old_ids))):
                        raise HTTPException(409,'같은 요청 ID로 다른 복습 범위를 만들 수 없습니다.')
                    if previous["status"]=="queued" and previous["attempts"]==0:
                        self.start();self.wake.set()
                    return self.result(connection,previous)
                if not self.configured:
                    raise HTTPException(503,'수업 정리본 API 설정이 필요합니다.')
                if connection.execute("SELECT 1 FROM course_review_jobs WHERE username=? AND status IN ('queued','processing')",(user['username'],)).fetchone():
                    raise HTTPException(409,'진행 중인 강의 복습이 끝난 뒤 다시 요청해 주세요.')
                snapshot,revision=self.snapshot(connection,user['username'],course_id,selected)
                # The same source/model result survives a lost response or a new
                # browser request ID. Changed sources still require explicit work.
                cached=connection.execute("SELECT * FROM course_review_jobs WHERE username=? AND course_id=? AND source_revision=? AND model=? AND status='completed' ORDER BY created_at DESC LIMIT 1",(user['username'],course_id,revision,self.model)).fetchone()
                if cached is not None:
                    return self.result(connection,cached)
                if not self.limiter.allow(('course-review',user['username']),6,3600):
                    raise HTTPException(429,'복습 요청이 많습니다. 잠시 후 다시 시도하세요.')
                timestamp=_now()
                connection.execute("INSERT INTO course_review_jobs(id,username,course_id,model,status,source_revision,source_manifest_json,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?,?,?)",(identifier,user['username'],course_id,self.model,revision,json.dumps(snapshot,ensure_ascii=False),timestamp,timestamp))
                connection.executemany('INSERT INTO course_review_sources(job_id,username,lecture_id) VALUES(?,?,?)',[(identifier,user['username'],item['lecture_id']) for item in snapshot['sessions']])
                result=self.result(connection,connection.execute('SELECT * FROM course_review_jobs WHERE id=?',(identifier,)).fetchone())
            self.start();self.wake.set()
            return result

        @app.get('/courses/{course_id}/reviews')
        def list_reviews(course_id:str,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100),user:dict=Depends(identity)):
            with self.database.connect() as connection:
                _course(connection,course_id,user['username'])
                total=connection.execute('SELECT count(*) FROM course_review_jobs WHERE course_id=? AND username=?',(course_id,user['username'])).fetchone()[0]
                rows=connection.execute('SELECT id,status,model,created_at,updated_at,completed_at,error_code FROM course_review_jobs WHERE course_id=? AND username=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(course_id,user['username'],limit,offset)).fetchall()
                return {'reviews':[dict(row) for row in rows],'total':total,'offset':offset,'next_offset':offset+len(rows) if offset+len(rows)<total else None}

        @app.get('/courses/{course_id}/reviews/{review_id}')
        def get_review(course_id:str,review_id:str,user:dict=Depends(identity)):
            with self.database.connect() as connection:
                _course(connection,course_id,user['username'])
                row=connection.execute('SELECT * FROM course_review_jobs WHERE id=? AND course_id=? AND username=?',(review_id,course_id,user['username'])).fetchone()
                if row is None:
                    raise HTTPException(404,'강의 복습을 찾을 수 없습니다.')
                return self.result(connection,row)
