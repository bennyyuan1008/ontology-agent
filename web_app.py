# -*- coding: utf-8 -*-
"""零依赖 HTTP 控制面：异常、建议和人工确认。"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from monitor_service import SQLiteMonitorStore


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def _html(self, body):
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("请求体过大")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", [100])[0])
            if parsed.path == "/":
                return self._html("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ontology Agent 控制台</title><style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:1100px;margin:32px auto;padding:0 16px;color:#172033;background:#f7f8fa}
h1{margin-bottom:6px}.muted{color:#6b7280}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:white;border:1px solid #e5e7eb;border-radius:10px;padding:16px;box-shadow:0 1px 2px #0000000d}
pre{white-space:pre-wrap;word-break:break-word;background:#f3f4f6;padding:10px;border-radius:6px;max-height:360px;overflow:auto}button{border:0;border-radius:6px;background:#2563eb;color:white;padding:7px 12px;cursor:pointer}button:hover{background:#1d4ed8}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body><h1>Ontology Agent 控制台</h1><p class="muted">只读查看异常与建议；建议确认后才创建任务。</p>
<div class="grid"><section class="card"><h2>异常</h2><button onclick="load()">刷新</button><pre id="anomalies">加载中...</pre></section>
<section class="card"><h2>建议</h2><p>在下方输入建议 ID、操作人后确认。</p><input id="rid" placeholder="recommendation_id" style="width:100%;box-sizing:border-box;padding:8px"><br><br>
<input id="actor" placeholder="操作人" value="operator" style="width:100%;box-sizing:border-box;padding:8px"><br><br><button onclick="confirmRec(true)">采纳并创建任务</button> <button onclick="confirmRec(false)">拒绝</button><pre id="recommendations">加载中...</pre></section>
<section class="card"><h2>任务</h2><pre id="tasks">加载中...</pre></section><section class="card"><h2>操作结果</h2><pre id="result">-</pre></section></div>
<script>
async function get(path){let r=await fetch(path);return await r.json()}
async function load(){let [a,r,t]=await Promise.all([get('/anomalies'),get('/recommendations'),get('/tasks')]);document.querySelector('#anomalies').textContent=JSON.stringify(a.items,null,2);document.querySelector('#recommendations').textContent=JSON.stringify(r.items,null,2);document.querySelector('#tasks').textContent=JSON.stringify(t.items,null,2)}
async function confirmRec(accepted){let id=document.querySelector('#rid').value.trim();let actor=document.querySelector('#actor').value.trim();let r=await fetch('/recommendations/'+encodeURIComponent(id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor,accepted})});document.querySelector('#result').textContent=JSON.stringify(await r.json(),null,2);load()}
load()
</script></body></html>""")
            if parsed.path == "/health":
                return self._json(200, {"ok": True})
            if parsed.path == "/anomalies":
                return self._json(200, {"items": store.list_anomalies(limit)})
            if parsed.path == "/recommendations":
                return self._json(200, {"items": store.list_recommendations(limit)})
            if parsed.path == "/tasks":
                return self._json(200, {"items": store.list_tasks(limit)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            feedback_prefix = "/anomalies/"
            if parsed.path.startswith(feedback_prefix) and parsed.path.endswith("/feedback"):
                anomaly_id = parsed.path[len(feedback_prefix):-len("/feedback")].strip("/")
                try:
                    payload = self._body()
                    actor = payload.get("actor")
                    if not isinstance(actor, str) or not actor.strip():
                        raise ValueError("actor 必须是非空字符串")
                    feedback = store.add_feedback(anomaly_id, actor, {
                        "label": payload.get("label", ""),
                        "note": str(payload.get("note", ""))[:500],
                    })
                    return self._json(200, {"feedback": feedback})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
            prefix = "/recommendations/"
            if not parsed.path.startswith(prefix) or not parsed.path.endswith("/confirm"):
                return self._json(404, {"error": "not found"})
            recommendation_id = parsed.path[len(prefix):-len("/confirm")].strip("/")
            try:
                payload = self._body()
                actor = payload.get("actor")
                accepted = payload.get("accepted")
                if not isinstance(accepted, bool):
                    raise ValueError("accepted 必须是布尔值")
                result = store.update_recommendation(
                    recommendation_id,
                    "accepted" if accepted else "rejected",
                    actor,
                    payload.get("note", ""),
                )
                task = store.create_task(result, payload.get("assignee", "")) if accepted else None
                store.audit("recommendation.confirmed", actor, {
                    "recommendation_id": recommendation_id,
                    "status": result["status"],
                    "task_id": task.get("task_id") if task else None,
                })
                return self._json(200, {"recommendation": result, "task": task})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc)})

        def log_message(self, fmt, *args):
            return

    return Handler


def serve(state_path="local/monitor_control.sqlite3", host="127.0.0.1", port=8787):
    store = SQLiteMonitorStore(state_path)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"ontology-agent control API: http://{host}:{port}")
    server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="启动 ontology-agent 轻量控制 API")
    ap.add_argument("--state", default="local/monitor_control.sqlite3")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    serve(args.state, args.host, args.port)


if __name__ == "__main__":
    main()
