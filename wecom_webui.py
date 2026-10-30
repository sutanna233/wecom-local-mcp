"""Local read-only WebUI for the WeCom message store.

Uses only Python's standard library and the existing indexed wecom_mcp queries.
The server binds to loopback only and serves no external assets.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import wecom_mcp as mcp

ROOT = Path(__file__).resolve().parent
HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>企业微信消息助手</title>
<style>
:root{--bg:#f4f6fa;--panel:#fff;--ink:#172033;--muted:#697386;--line:#e3e8f0;--accent:#136f63;--accent2:#e4f4ef;--shadow:0 8px 30px rgba(25,40,65,.07)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:#132d2a;color:#fff;padding:22px 28px;display:flex;align-items:center;justify-content:space-between;gap:20px}header h1{font-size:21px;margin:0 0 4px}header p{margin:0;color:#b8d2cc}.status{padding:7px 12px;border-radius:18px;background:#28534d;white-space:nowrap;font-size:12px}.status.off{background:#5d5144}
main{max-width:1440px;margin:auto;padding:24px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.card{padding:17px 19px}.card b{display:block;font-size:24px;margin-top:5px}.label{color:var(--muted);font-size:12px}.panel{padding:18px;margin-bottom:18px}.panel h2{font-size:16px;margin:0 0 14px}.grid{display:grid;grid-template-columns:1.3fr 1fr 1fr 1fr 1fr;gap:10px}label{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:12px}input,select,button{font:inherit}input,select{border:1px solid #ccd5e1;border-radius:8px;padding:9px 10px;background:#fff;color:var(--ink);min-width:0}button{border:0;border-radius:8px;padding:10px 15px;background:var(--accent);color:#fff;cursor:pointer}button.secondary{background:#e8edf3;color:#344052}button:disabled{opacity:.45;cursor:default}.actions{display:flex;gap:8px;align-items:flex-end}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:500;white-space:nowrap}tbody tr{cursor:pointer}tbody tr:hover{background:#f1f8f6}.msg{max-width:600px;white-space:pre-wrap;word-break:break-word}.tag{display:inline-block;padding:2px 7px;border-radius:10px;background:var(--accent2);color:var(--accent);font-size:11px;white-space:nowrap}.tag.gray{background:#edf0f4;color:#6b7380}.pager{display:flex;align-items:center;justify-content:space-between;margin-top:14px;color:var(--muted);font-size:12px}.pager div{display:flex;gap:8px}.bars{display:grid;gap:9px}.bar{display:grid;grid-template-columns:180px 1fr 65px;gap:10px;align-items:center}.bar span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{height:8px;background:#e8edf2;border-radius:8px;overflow:hidden}.fill{height:100%;background:#51a892}.empty{padding:35px;text-align:center;color:var(--muted)}dialog{border:0;border-radius:15px;max-width:850px;width:90vw;max-height:85vh;box-shadow:0 25px 90px #0004}dialog::backdrop{background:#10231f88}.detail{padding:22px}.detail h2{margin:0 0 12px}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;color:var(--muted);font-size:12px}.detail pre{white-space:pre-wrap;word-break:break-word;background:#f6f8fa;border-radius:10px;padding:16px;max-height:45vh;overflow:auto;font:13px/1.65 inherit}.close{float:right;background:#e8edf3;color:#333}.small{font-size:12px;color:var(--muted)}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr 1fr}.msg{max-width:350px}.bar{grid-template-columns:110px 1fr 50px}main{padding:14px}header{padding:18px}.meta{grid-template-columns:1fr}}
</style></head><body>
<header><div><h1>企业微信消息助手</h1><p>本地只读 · 离线数据 · 不发送消息</p></div><span id="collector" class="status">采集状态读取中</span></header>
<main>
<section class="cards"><div class="card"><span class="label">本地消息</span><b id="total">—</b></div><div class="card"><span class="label">会话</span><b id="rooms">—</b></div><div class="card"><span class="label">最近消息</span><b id="latest" style="font-size:16px">—</b></div><div class="card"><span class="label">全文索引</span><b id="fts">—</b></div></section>
<section class="panel"><h2>消息查询</h2><form id="search" class="grid">
<label>关键词<input id="query" placeholder="eh08 / JPH / 文件名"></label><label>会话<select id="room"><option value="">全部会话</option></select></label><label>发送人<input id="sender" placeholder="名称或 ID"></label><label>开始日期<input id="start" type="date"></label><label>结束日期<input id="end" type="date"></label>
<label>消息类型<select id="type"><option value="">全部类型</option><option value="0">文本/混合</option><option value="2">文本</option><option value="13">在线表格</option><option value="14">图片</option><option value="35">链接分享</option><option value="1002">@提及</option></select></label><div class="actions"><button type="submit">查询</button><button type="button" class="secondary" id="reset">重置</button></div>
</form></section>
<section class="panel"><div style="display:flex;justify-content:space-between;align-items:center"><h2 id="resultTitle">消息</h2><span id="resultMeta" class="small"></span></div><div class="tablewrap"><table><thead><tr><th>时间</th><th>会话 / 发送人</th><th>类型</th><th>内容</th><th>标记</th></tr></thead><tbody id="messages"></tbody></table></div><div id="empty" class="empty" hidden>没有匹配的消息</div><div class="pager"><span id="pageInfo"></span><div><button class="secondary" id="prev">上一页</button><button class="secondary" id="next">下一页</button></div></div></section>
<section class="panel"><h2>聚合统计</h2><div class="grid" style="grid-template-columns:220px 1fr"><label>统计维度<select id="group"><option value="day">按日期</option><option value="conversation">按会话</option><option value="sender">按发送人</option><option value="msgtype">按消息类型</option></select></label><div id="bars" class="bars"></div></div></section>
</main>
<dialog id="dialog"><div class="detail"><button class="close" id="close">关闭</button><h2>消息详情</h2><div id="detailMeta" class="meta"></div><pre id="detailContent"></pre><div id="detailExtra" class="small"></div></div></dialog>
<script>
const $=id=>document.getElementById(id);const state={offset:0,limit:20,total:0};
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
async function api(path,params={}){const q=new URLSearchParams();Object.entries(params).forEach(([k,v])=>{if(v!==''&&v!==null&&v!==undefined)q.set(k,v)});const r=await fetch(path+(q.size?'?'+q:''),{headers:{'Accept':'application/json'}});const data=await r.json();if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data}
function fmt(n){return Number(n||0).toLocaleString('zh-CN')}
async function loadStats(){const d=await api('/api/stats');$('total').textContent=fmt(d.messages);$('rooms').textContent=fmt(d.conversations);$('latest').textContent=d.latest_time||'—';$('fts').textContent=d.fts_ready?'已启用':'回退';const c=$('collector');c.textContent=d.collector.running?'采集中 · PID '+d.collector.pid:'后台未运行';c.className='status'+(d.collector.running?'':' off')}
async function loadConversations(){const d=await api('/api/conversations',{limit:200});const select=$('room');const current=select.value;d.conversations.forEach(c=>{const o=el('option','',c.conversation+' · '+fmt(c.message_count));o.value=c.conversation_id;select.appendChild(o)});select.value=current}
function filters(){return{query:$('query').value.trim(),conversation_id:$('room').value,sender:$('sender').value.trim(),start_time:$('start').value,end_time:$('end').value,msgtype:$('type').value}}
async function loadMessages(){const p={...filters(),limit:state.limit,offset:state.offset};const d=await api('/api/messages',p);state.total=d.total;const body=$('messages');body.replaceChildren();$('empty').hidden=d.count>0;d.messages.forEach(m=>{const tr=el('tr');tr.appendChild(el('td','small',m.time||'—'));const who=el('td');who.appendChild(el('div','',m.conversation));who.appendChild(el('div','small',m.sender));tr.appendChild(who);tr.appendChild(el('td','',m.type_name));const content=el('td','msg',m.content.length>240?m.content.slice(0,240)+'…':m.content);tr.appendChild(content);const tags=el('td');if(m.dup_count>1)tags.appendChild(el('span','tag','重复 ×'+m.dup_count));if(m.mention_all){if(tags.childNodes.length)tags.appendChild(document.createTextNode(' '));tags.appendChild(el('span','tag','@所有人'))}if(m.flag){if(tags.childNodes.length)tags.appendChild(document.createTextNode(' '));tags.appendChild(el('span','tag gray','flag '+m.flag))}tr.appendChild(tags);tr.addEventListener('click',()=>showMessage(m.msgid));body.appendChild(tr)});$('resultMeta').textContent='共 '+fmt(d.total)+' 条 · 本页 '+d.count+' 条';$('pageInfo').textContent='第 '+Math.floor(d.offset/state.limit+1)+' 页';$('prev').disabled=d.offset===0;$('next').disabled=!d.has_more;$('next').textContent=d.has_more?'下一页':'没有更多'}
async function showMessage(id){const m=await api('/api/message',{msgid:id});const meta=$('detailMeta');meta.replaceChildren();[['会话',m.conversation],['发送人',m.sender],['时间',m.time],['类型',m.type_name],['消息 ID',m.msgid],['重复组',m.dup_count+' 条']].forEach(([k,v])=>{const d=el('div');d.appendChild(el('div','label',k));d.appendChild(el('div','',String(v)));meta.appendChild(d)});$('detailContent').textContent=m.content||'（无文本内容）';const extra=[];if(m.at_user&&m.at_user.length)extra.push('提及：'+m.at_user.join(' '));if(m.flag)extra.push('flag：'+m.flag+' / bits：'+JSON.stringify(m.flag_bits));$('detailExtra').textContent=extra.join('\n');$('dialog').showModal()}
async function loadCount(){const p={...filters(),group_by:$('group').value};delete p.query;const d=await api('/api/count',p);const bars=$('bars');bars.replaceChildren();const groups=d.groups.slice(0,12);if(!groups.length){bars.appendChild(el('div','empty','暂无统计数据'));return}const max=Math.max(...groups.map(g=>g.count),1);groups.forEach(g=>{const key=g.day||g.conversation||g.sender||g.type_name||g.conversation_id||'未分组';const row=el('div','bar');row.appendChild(el('span','',key));const track=el('div','track');track.appendChild(el('div','fill')).style.width=(g.count/max*100)+'%';row.appendChild(track);row.appendChild(el('span','',fmt(g.count)));bars.appendChild(row)})}
async function refresh(){try{await Promise.all([loadStats(),loadMessages(),loadCount()])}catch(e){alert(e.message)}}
$('search').addEventListener('submit',e=>{e.preventDefault();state.offset=0;refresh()});$('reset').addEventListener('click',()=>{['query','sender','start','end','type'].forEach(id=>$(id).value='');$('room').value='';state.offset=0;refresh()});$('prev').addEventListener('click',()=>{state.offset=Math.max(0,state.offset-state.limit);refresh()});$('next').addEventListener('click',()=>{if(state.offset+state.limit<state.total){state.offset+=state.limit;refresh()}});$('group').addEventListener('change',loadCount);$('close').addEventListener('click',()=>$('dialog').close());$('dialog').addEventListener('click',e=>{if(e.target===$('dialog'))$('dialog').close()});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('dialog').open)$('dialog').close()});
Promise.all([loadStats(),loadConversations(),refresh()]).catch(e=>alert(e.message));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WeComWebUI/1.4.0"

    def log_message(self, fmt, *args):
        # Keep request logs free of message query strings and content.
        print(f"WebUI {self.command} {self.path.split('?')[0]}")

    def _allowed_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0]
        return host in {"127.0.0.1", "localhost", "::1"}

    def _send(self, value, status=200, content_type="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _params(self):
        raw = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        return {key: values[-1] for key, values in raw.items()}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._allowed_host():
            self._send({"error": "loopback host required"}, 403)
            return
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send(HTML.encode("utf-8"), content_type="text/html; charset=utf-8")
            elif path == "/api/stats":
                self._send(self.stats())
            elif path == "/api/conversations":
                self._send(mcp.list_conversations(self._params()))
            elif path == "/api/messages":
                params = self._params()
                if params.get("query"):
                    self._send(mcp.search_messages(params))
                else:
                    self._send(mcp.recent_messages(params))
            elif path == "/api/message":
                self._send(mcp.get_message(self._params()))
            elif path == "/api/count":
                self._send(mcp.count_messages(self._params()))
            else:
                self._send({"error": "not found"}, 404)
        except (ValueError, sqlite3.Error) as exc:
            self._send({"error": str(exc)}, 400)
        except Exception as exc:
            self._send({"error": f"internal error: {type(exc).__name__}"}, 500)

    @staticmethod
    def stats() -> dict:
        with closing(mcp.connect()) as con:
            messages, latest, rooms = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(msgtime),0),COUNT(DISTINCT roomid) FROM messages"
            ).fetchone()
            fts_ready = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
            ).fetchone() is not None
        collector = {"running": False, "pid": None, "heartbeat": None}
        pid_path = ROOT / "logs" / "collector.pid"
        try:
            data = json.loads(pid_path.read_text(encoding="utf-8"))
            heartbeat = int(data.get("heartbeat") or 0)
            collector = {
                "running": bool(heartbeat and time.time() - heartbeat < 90),
                "pid": data.get("pid"),
                "heartbeat": heartbeat,
            }
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {
            "messages": messages,
            "conversations": rooms,
            "latest_time": mcp.format_time(latest),
            "fts_ready": fts_ready,
            "collector": collector,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("WebUI only supports loopback hosts")
    if args.db:
        mcp.DB_PATH = Path(args.db).resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"WebUI: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
