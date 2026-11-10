#!/usr/bin/env python3
"""Live view of kb.sqlite at http://127.0.0.1:8765. Local only, read only, counts only."""
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "kb.sqlite")
PORT = 8765
LABELS = {
    "gmail:acct1": "Gmail account 1", "gmail:acct2": "Gmail account 2", "claude_code": "Claude chats",
    "gym": "Gym", "github": "GitHub", "gcal": "Calendar", "uni": "Uni", "contacts": "Contacts", "career": "Career",
    "projects": "Projects", "cv": "CV", "self": "System notes", "job_tracker": "Job tracker",
}


def snapshot():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        counts = dict(conn.execute("SELECT source, COUNT(*) FROM docs GROUP BY source").fetchall())
        last = conn.execute("SELECT watermark FROM ingest_state WHERE source='_meta' AND key='last_ingest'").fetchone()
    finally:
        conn.close()
    names = list(LABELS) + sorted(s for s in counts if s not in LABELS)
    rows = [{"name": LABELS.get(s, s), "count": counts.get(s, 0)} for s in names]
    rows.sort(key=lambda r: -r["count"])
    size = os.path.getsize(DB) + (os.path.getsize(DB + "-wal") if os.path.exists(DB + "-wal") else 0)
    return {"total": sum(counts.values()), "mb": round(size / 1048576, 1), "last": last[0] if last else None, "rows": rows}


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Second Brain Database</title>
<style>:root{--bg:#f5f7fa;--c:#fff;--f:#17202b;--m:#5d6b7a;--l:#dbe1e8;--a:#2f5fd0}
@media(prefers-color-scheme:dark){:root{--bg:#10161e;--c:#19212c;--f:#e9eef4;--m:#9dabba;--l:#2d3845;--a:#86a9f5}}
body{margin:0;background:var(--bg);color:var(--f);font:14px system-ui,sans-serif}main{max-width:760px;margin:auto;padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.c{background:var(--c);border:1px solid var(--l);border-radius:12px;padding:14px;min-width:0}h2{font-size:15px;font-weight:600;margin:0 0 2px}.s{font-size:12px;color:var(--m);margin-bottom:10px}
.r{display:grid;grid-template-columns:118px 1fr 44px;gap:8px;align-items:center;font-size:12px;margin:5px 0}.b{height:10px;background:var(--a);border-radius:3px;transition:width .4s}.n{text-align:right;font-variant-numeric:tabular-nums;color:var(--m)}
.f{display:grid;grid-template-columns:100px 1fr;gap:8px;font-size:12px;padding:4px 0;border-top:1px solid var(--l)}.f code{font:12px ui-monospace,monospace}.f span{color:var(--m)}</style>
<main><section class=c><h2>What's inside: <span id=t>...</span> rows</h2><div class=s id=i>Loading</div><div id=rows></div></section>
<section class=c><h2>Main table: docs</h2><div class=s>Every item from every source is one row shaped like this.</div>
<div class=f><code>id</code><span>Row number</span></div><div class=f><code>source</code><span>Where it came from, like gmail:acct1 or uni</span></div>
<div class=f><code>source_id</code><span>Its ID there, so re-runs don't duplicate it</span></div><div class=f><code>kind</code><span>email, chat, exam, weak_point, person, lead</span></div>
<div class=f><code>ts</code><span>Its date</span></div><div class=f><code>title</code><span>Subject or name</span></div><div class=f><code>content</code><span>The text, with keys and passwords masked</span></div>
<div class=f><code>metadata</code><span>Link to the original, status, sender</span></div><div class=f><code>superseded_by</code><span>Marks a row a newer one replaced</span></div></section>
<section class=c><h2>Search index: docs_fts</h2><div class=s>A word index over title and content. It's why search takes milliseconds and uses no tokens.</div></section>
<section class=c><h2>Bookkeeping: ingest_state</h2><div class=s>Remembers where each source stopped, so updates only fetch what's new.</div></section></main>
<script>const R=document.getElementById('rows');
async function tick(){try{const d=await (await fetch('/data',{cache:'no-store'})).json();document.getElementById('t').textContent=d.total.toLocaleString();
document.getElementById('i').textContent='kb.sqlite, '+d.mb+' MB. Live. Last full update: '+(d.last||'never').replace('T',' ');
const max=Math.max(1,...d.rows.map(r=>r.count));R.replaceChildren(...d.rows.map(r=>{const e=document.createElement('div');e.className='r';const a=document.createElement('span');a.textContent=r.name;
const w=document.createElement('div');const b=document.createElement('div');b.className='b';b.style.width=r.count?Math.max(1,100*r.count/max)+'%':'0';w.append(b);const n=document.createElement('span');n.className='n';n.textContent=r.count.toLocaleString();e.append(a,w,n);return e}))}
catch(e){document.getElementById('i').textContent='Viewer lost contact with the database. Retrying.'}}
tick();setInterval(tick,2000)</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            body, kind = json.dumps(snapshot()).encode(), "application/json"
        elif self.path == "/":
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
