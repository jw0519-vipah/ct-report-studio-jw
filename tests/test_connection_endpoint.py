import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as target

MODE = {"status": 200}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_response(404); self.end_headers(); return
        s = MODE["status"]
        self.send_response(s)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", f"req-{s}")
        if s == 429:
            self.send_header("Retry-After", "3")
        self.end_headers()
        if s == 200:
            body={"object":"list","data":[]}
        elif s == 401:
            body={"error":{"message":"Incorrect API key","code":"invalid_api_key","type":"invalid_request_error"}}
        elif s == 429:
            body={"error":{"message":"No credits","code":"credit_balance_exhausted","type":"insufficient_quota"}}
        else:
            body={"error":{"message":"upstream"}}
        self.wfile.write(json.dumps(body).encode())
    def log_message(self, *_): pass

async def main():
    server=HTTPServer(("127.0.0.1",0), Handler)
    port=server.server_address[1]
    threading.Thread(target=server.serve_forever,daemon=True).start()
    target.OPENAI_BASE_URL=f"http://127.0.0.1:{port}"
    os.environ["OPENAI_API_KEY"]="sk-test"
    transport=httpx.ASGITransport(app=target.app)
    async with httpx.AsyncClient(transport=transport,base_url="http://test") as c:
        for status, expected_kind in [(200,None),(401,"authentication_error"),(429,"quota_error")]:
            MODE["status"]=status
            r=await c.post("/api/openai/test")
            j=r.json()
            if status==200:
                assert r.status_code==200 and j["ok"] is True
            else:
                assert r.status_code==status and j["error"]["kind"]==expected_kind
            print(status, "PASS", j.get("message") or j.get("error",{}).get("title"))
    server.shutdown()

asyncio.run(main())
