"""Synthetic timetable recognition: no credentials, private classes or paid calls."""
import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image
from server.db import Database
from server.settings import Settings
from server.review_parse import TimetableRecognizer, parse_classes, MAX_IMAGE

ROW={"subject":"합성 과목","day":"Thu","start":"15시","end":"14:00","room":""}

def reply(content=None, **extra):
    return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":content or json.dumps([ROW],ensure_ascii=False)},**extra}]})


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="reminder-parse-")
        self.settings=Settings(data_dir=Path(self.temp.name)/"data",model_cache_dir=Path(self.temp.name)/"models",mindlogic_api_key="synthetic-key")
        self.database=Database(self.settings.database_path,self.settings.accounts);self.database.initialize()
        self.requests=[]
        def handle(req):
            self.requests.append(req);return reply()
        self.recognizer=TimetableRecognizer(self.settings,self.database,transport=httpx.MockTransport(handle))

    def tearDown(self): self.temp.cleanup()

    async def test_configured_model_and_cleaned_rows(self):
        result=await self.recognizer.recognize("user-alpha",text="합성 시간표")
        self.assertEqual(result["classes"][0]["day"],4)
        self.assertEqual(result["classes"][0]["start"],"15:00")
        self.assertEqual(result["classes"][0]["end"],"")
        request=self.requests[0];self.assertEqual(request.url.host,"factchat-cloud.mindlogic.ai")
        self.assertEqual(json.loads(request.content)["model"],"gpt-6-luna")
        with self.database.connect() as c:
            self.assertEqual(c.execute("SELECT count FROM review_parse_usage").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM review_items").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM review_parse_leases").fetchone()[0],0)

    async def test_images_remain_in_memory(self):
        b=io.BytesIO();Image.new("RGB",(30,20),"white").save(b,format="PNG")
        before={p.relative_to(self.settings.data_dir) for p in self.settings.data_dir.rglob("*")}
        await self.recognizer.recognize("user-alpha",image=b.getvalue(),mime="image/png")
        self.assertEqual(before,{p.relative_to(self.settings.data_dir) for p in self.settings.data_dir.rglob("*")})
        content=json.loads(self.requests[0].content)["messages"][1]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_invalid_inputs_do_not_call_or_consume_quota(self):
        for kw in [{"text":"x"*6001},{"text":""},{"text":5},{"image":b"bad","mime":"image/png"},{"image":b"x"*(MAX_IMAGE+1),"mime":"image/png"}]:
            with self.assertRaises(HTTPException): await self.recognizer.recognize("user-alpha",**kw)
        self.assertEqual(self.requests,[])
        with self.database.connect() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM review_parse_usage").fetchone()[0],0)

    async def test_quota_survives_new_recognizer_and_is_per_owner(self):
        for _ in range(20):await self.recognizer.recognize("user-alpha",text="합성")
        replacement=TimetableRecognizer(self.settings,self.database,transport=self.recognizer.transport)
        with self.assertRaises(HTTPException) as e:await replacement.recognize("user-alpha",text="합성")
        self.assertEqual(e.exception.status_code,429)
        await replacement.recognize("user-beta",text="합성")
        self.assertEqual(len(self.requests),21)

    async def test_active_owner_lease_and_expired_recovery(self):
        first=self.recognizer.reserve("user-alpha")
        with self.assertRaises(HTTPException) as e:self.recognizer.reserve("user-alpha")
        self.assertEqual(e.exception.status_code,429)
        with self.database.connect() as c:c.execute("UPDATE review_parse_leases SET expires_at=0")
        second=self.recognizer.reserve("user-alpha")
        self.recognizer.release("user-alpha",first)
        with self.database.connect() as c:self.assertEqual(c.execute("SELECT lease_id FROM review_parse_leases").fetchone()[0],second)
        self.recognizer.release("user-alpha",second)

    async def test_provider_failure_timeout_and_truncation_are_safe(self):
        for transport,code in [(lambda req:httpx.Response(500,text="PRIVATE_GATEWAY_BODY"),502),
                               (lambda req:reply("not json"),422),
                               (lambda req:httpx.Response(200,json=[]),422),
                               (lambda req:httpx.Response(200,json={"choices":[None]}),422),
                               (lambda req:reply(finish_reason="length"),422),
                               (lambda req:httpx.Response(200,content=b"x"*300000),422)]:
            self.recognizer.transport=httpx.MockTransport(transport)
            with self.assertRaises(HTTPException) as e:await self.recognizer.recognize("user-alpha",text="합성")
            self.assertEqual(e.exception.status_code,code);self.assertNotIn("PRIVATE",str(e.exception))
        def timeout(req):raise httpx.ReadTimeout("private")
        self.recognizer.transport=httpx.MockTransport(timeout)
        with self.assertRaises(HTTPException) as e:await self.recognizer.recognize("user-alpha",text="합성")
        self.assertEqual(e.exception.status_code,504)

    async def test_not_configured_has_no_network(self):
        settings=Settings(data_dir=self.settings.data_dir,model_cache_dir=self.settings.model_cache_dir)
        recognizer=TimetableRecognizer(settings,self.database,transport=self.recognizer.transport)
        self.assertFalse(recognizer.capabilities()["enabled"])
        with self.assertRaises(HTTPException) as e:await recognizer.recognize("user-alpha",text="합성")
        self.assertEqual(e.exception.status_code,503);self.assertFalse(self.requests)

    async def test_kst_quota_day_at_utc_boundary(self):
        self.recognizer.clock=lambda: 1790697600  # 2026-09-29 16:00 UTC -> Sep30 KST
        lease=self.recognizer.reserve("user-alpha")
        with self.database.connect() as c:self.assertEqual(c.execute("SELECT day FROM review_parse_usage").fetchone()[0],"2026-09-30")
        self.recognizer.release("user-alpha",lease)

    def test_tolerant_json_and_invalid_response(self):
        self.assertEqual(parse_classes("```json\n"+json.dumps([ROW])+"\n```")[0]["subject"],ROW["subject"])
        for data in ["[]","{}","not json",json.dumps([{ "subject":"" }])]:
            with self.assertRaises(ValueError):parse_classes(data)

    def test_route_requires_auth_and_content_type_and_does_not_reflect_input(self):
        app=FastAPI()
        def identity(request:Request):
            if request.headers.get("authorization")!="Bearer synthetic":raise HTTPException(401,"로그인 필요")
            return {"username":"user-alpha"}
        self.recognizer.install(app,identity=identity)
        with TestClient(app) as client:
            self.assertEqual(client.post("/review/timetable/parse",json={"text":"합성"}).status_code,401)
            headers={"Authorization":"Bearer synthetic"}
            self.assertEqual(client.post("/review/timetable/parse",content=b"bad",headers={**headers,"content-type":"application/pdf"}).status_code,415)
            response=client.post("/review/timetable/parse",json={"text":"합성"},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.post("/review/timetable/parse",json={"text":"x"*6001},headers=headers).status_code,400)
            self.assertEqual(client.post("/review/timetable/parse",content=b"x"*(MAX_IMAGE+1),headers={**headers,"content-type":"image/png"}).status_code,413)

if __name__=="__main__":unittest.main()
