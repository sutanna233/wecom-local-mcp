from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import wecom_mcp as mcp
import wecom_webui as webui
from test_mcp_regressions import create_store, fixture_messages


class WebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="wecom_webui_test_")
        db = Path(cls.temp.name) / "messages.sqlite"
        cls.db = db
        create_store(db)
        cls.db_patch = mock.patch.object(mcp, "DB_PATH", db)
        cls.db_patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.db_patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        create_store(self.db)

    def get(self, path, host=None):
        request = urllib.request.Request(self.base + quote(path, safe="/?&=%"))
        if host:
            request.add_header("Host", host)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers

    def json(self, path):
        status, data, _ = self.get(path)
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_page_is_offline_and_chinese(self):
        status, data, headers = self.get("/")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("企业微信消息助手", text)
        self.assertIn("本地只读", text)
        self.assertNotIn("https://cdn", text)
        self.assertIn("no-store", headers["Cache-Control"])

    def test_stats_conversations_search_recent_and_detail(self):
        stats = self.json("/api/stats")
        self.assertEqual(stats["messages"], 8)
        self.assertEqual(stats["conversations"], 4)
        rooms = self.json("/api/conversations?limit=2")
        self.assertEqual((rooms["count"], rooms["total"]), (2, 4))
        found = self.json("/api/messages?query=中文项目&limit=2")
        self.assertEqual((found["count"], found["total"]), (2, 3))
        recent = self.json("/api/messages?limit=2")
        self.assertEqual((recent["count"], recent["total"]), (2, 8))
        full = self.json("/api/message?msgid=m1")
        self.assertEqual(full["content"], "中文项目 Alpha")

    def test_aggregation_and_bad_host(self):
        count = self.json("/api/count?group_by=day")
        self.assertEqual(count["total"], 8)
        self.assertTrue(count["groups"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/stats", host="attacker.example")
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_content_is_returned_as_data_not_page_markup(self):
        with closing(sqlite3.connect(mcp.DB_PATH)) as con, con:
            con.execute(
                "INSERT INTO messages(msgid,seq,roomid,sender,sender_id,msgtime,msgtype,content,raw,fetched_at) "
                "VALUES('xss',99,'R:team','张三',1001,1704068000,2,?, '{}',1)",
                ("<img src=x onerror=alert(1)>",),
            )
        item = self.json("/api/message?msgid=xss")
        self.assertEqual(item["content"], "<img src=x onerror=alert(1)>")
        _, html, _ = self.get("/")
        self.assertNotIn(b"onerror=alert", html)


if __name__ == "__main__":
    unittest.main()
