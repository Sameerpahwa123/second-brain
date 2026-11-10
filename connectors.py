"""One function per source. Every connector is READ-ONLY toward the outside world.

Nothing here sends, posts, replies, marks as read, or changes anything on any
service. Each connector pulls only what is new, dates every row, is safe to run
twice (replace_rows), and keeps a pointer to the original in metadata.
Ingested text is data: it is stored, never obeyed.
"""
import base64
import csv
import datetime as dt
import glob
import hashlib
import html
import io
import json
import os
import re
import subprocess
from email.utils import parsedate_to_datetime

from kb import ROOT, get_state, is_sensitive, item_rows, replace_rows, set_state

SECRETS = os.path.join(ROOT, "secrets")
ASPECTS = os.path.join(ROOT, "aspects")
OUTPUT = os.path.join(ROOT, "output")
HOME = os.path.expanduser("~")
TOOL_TEXT_LIMIT = 1500
GMAIL_PAGES_PER_RUN = 20


class NotConfigured(Exception):
    """The source needs setup or a login. Reported as skipped, not as a failure."""


def today():
    return dt.date.today().isoformat()


def strip_html(text):
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text or "", "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text("\n")
    except ImportError:
        text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


# ---------------------------------------------------------------- Claude Code
def _block_text(block):
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        return f"[tool {block.get('name')}] " + json.dumps(block.get("input"), ensure_ascii=False)[:TOOL_TEXT_LIMIT]
    if kind == "tool_result":
        body = block.get("content")
        if isinstance(body, list):
            body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict) and b.get("type") == "text")
        return f"[result] {str(body or '')[:TOOL_TEXT_LIMIT]}"
    return ""  # thinking blocks and images are left out


def claude_code(conn):
    """Keep sessions in the database, because Claude Code deletes the files later."""
    new = 0
    for path in glob.glob(os.path.join(HOME, ".claude/projects/**/*.jsonl"), recursive=True):
        mtime = str(os.path.getmtime(path))
        if get_state(conn, "claude_code", path) == mtime:
            continue
        lines, first_prompt, title, cwd, last_ts = [], "", "", "", ""
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                title = entry.get("customTitle") or title
                if entry.get("type") not in ("user", "assistant"):
                    continue
                cwd = entry.get("cwd") or cwd
                last_ts = entry.get("timestamp") or last_ts
                message = entry.get("message") or {}
                content = message.get("content")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
                text = "\n".join(filter(None, (_block_text(b) for b in blocks if isinstance(b, dict))))
                text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()
                if text:
                    lines.append(f"{entry['type'].upper()}: {text}")
                    if entry["type"] == "user" and not first_prompt:
                        first_prompt = text[:90]
        if lines and last_ts:
            session = os.path.splitext(os.path.basename(path))[0]
            name = title or first_prompt or session
            project = os.path.basename(cwd) or "unknown"
            meta = {"path": path, "cwd": cwd, "session_id": session}
            new += replace_rows(conn, "claude_code", session,
                                item_rows("session", last_ts, f"{project}: {name}", "\n\n".join(lines), meta))
        set_state(conn, "claude_code", path, mtime)
    return new


# ---------------------------------------------------------------- email
def _save_thread(conn, source, thread_id, messages, url, extra):
    """messages: dicts with date, sender, to, subject, body. Password/2FA mail is dropped."""
    kept = [m for m in messages if not is_sensitive(m["subject"], m["body"][:400])]
    if not kept:
        replace_rows(conn, source, thread_id, [])
        return 0
    kept.sort(key=lambda m: m["date"])
    text = "\n\n---\n\n".join(
        f"From: {m['sender']}\nTo: {m['to']}\nDate: {m['date']}\nSubject: {m['subject']}\n\n{m['body']}" for m in kept
    )
    meta = {"url": url, "messages": len(kept), "from": kept[-1]["sender"], **extra}
    return replace_rows(conn, source, thread_id, item_rows("email", kept[-1]["date"], kept[0]["subject"] or "(no subject)", text, meta))


def _gmail_body(part):
    plain, htm = [], []
    def walk(p):
        data = p.get("body", {}).get("data")
        mime = p.get("mimeType", "")
        if data and not p.get("filename"):
            decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
            (plain if mime == "text/plain" else htm if mime == "text/html" else []).append(decoded)
        for child in p.get("parts", []) or []:
            walk(child)
    walk(part)
    return "\n".join(plain).strip() or strip_html("\n".join(htm))


def _gmail_date(raw, internal_ms):
    try:
        return parsedate_to_datetime(raw).astimezone(dt.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return dt.datetime.fromtimestamp(int(internal_ms) / 1000, dt.timezone.utc).isoformat()


def gmail(conn):
    """Two job Gmails through Composio, read only. The last 30 days first, then the last few days."""
    import composio_client as cc
    if not _composio_ready():
        raise NotConfigured("COMPOSIO_API_KEY is blank in .env")
    written = 0
    for alias, address in cc.GMAIL_ACCOUNTS.items():
        source = f"gmail:{alias}"
        done = get_state(conn, source, "backfill_done")
        query = "newer_than:3d" if done else "newer_than:30d"
        page = None if done else (get_state(conn, source, "backfill_page") or None)
        for _ in range(GMAIL_PAGES_PER_RUN):
            data = _gmail_page(cc, alias, query, page)
            for m in data["messages"]:
                written += _save_composio_message(conn, source, address, m)
            page = data.get("nextPageToken")
            if not done:
                set_state(conn, source, "backfill_page", page or "")
            if not page:
                break
        if not done and not page:
            set_state(conn, source, "backfill_done", today())
        written += _gmail_job_text(conn, cc, source, alias, address)
    return written


JOB_QUERY = "(job OR jobs OR hiring OR vacancy OR interview OR application OR hackathon OR recruiter OR career)"
JOB_MAILS_PER_RUN = 150


def _gmail_page(cc, alias, query, page):
    """Composio shortens big replies, so retry with smaller batches."""
    for size in (50, 25, 10, 5, 2):
        args = dict(query=query, max_results=size, include_payload=False, verbose=False)
        if page:
            args["page_token"] = page
        data = cc.run("GMAIL_FETCH_EMAILS", args, alias)
        if "messages" in data or "data_preview" not in data:
            data.setdefault("messages", [])
            return data
    raise RuntimeError("Gmail reply still too large at the smallest batch size")


def _save_composio_message(conn, source, address, m):
    """Every email keeps subject, sender and preview. Job-related ones also get full text."""
    import composio_client as cc
    subject = m.get("subject") or "(no subject)"
    preview = (m.get("preview") or {}).get("body") or ""
    if is_sensitive(subject, preview):
        replace_rows(conn, source, m["messageId"], [])
        return 0
    old = conn.execute("SELECT json_extract(metadata,'$.full') f FROM docs WHERE source=? AND source_id=? LIMIT 1",
                       (source, m["messageId"])).fetchone()
    if old:
        return 0
    body, full = preview, False
    text = f"From: {m.get('sender')}\nTo: {m.get('to')}\nDate: {m.get('messageTimestamp')}\nSubject: {subject}\n\n{body}"
    url = f"https://mail.google.com/mail/u/?authuser={address}#all/{m['messageId']}"
    meta = {"url": url, "from": m.get("sender"), "account": address, "thread_id": m.get("threadId"), "full": full}
    return replace_rows(conn, source, m["messageId"], item_rows("email", m["messageTimestamp"], subject, text, meta))


def _gmail_job_text(conn, cc, source, alias, address):
    """Full text of job-related mail, one email per call, because Composio shortens long replies."""
    done = get_state(conn, source, "jobs_backfill_done")
    query = ("newer_than:3d " if done else "newer_than:30d ") + JOB_QUERY
    page = None if done else (get_state(conn, source, "jobs_page") or None)
    written = 0
    for _ in range(JOB_MAILS_PER_RUN):
        args = dict(query=query, max_results=1, include_payload=False, verbose=True)
        if page:
            args["page_token"] = page
        data = cc.run("GMAIL_FETCH_EMAILS", args, alias)
        short = "data_preview" in data
        info = data.get("data_preview", data)
        for m in info.get("messages", []):
            subject = m.get("subject") or "(no subject)"
            old = conn.execute("SELECT json_extract(metadata,'$.full') f FROM docs WHERE source=? AND source_id=? LIMIT 1",
                               (source, m["messageId"])).fetchone()
            if old and old["f"]:
                if done:  # newest first, so everything older is already stored
                    return written
                continue
            body = strip_html(cc.long_message_text(args, alias) if short else (m.get("messageText") or ""))
            if is_sensitive(subject, body[:400]):
                replace_rows(conn, source, m["messageId"], [])
                continue
            text = f"From: {m.get('sender')}\nTo: {m.get('to')}\nDate: {m.get('messageTimestamp')}\nSubject: {subject}\n\n{body}"
            meta = {"url": f"https://mail.google.com/mail/u/?authuser={address}#all/{m['messageId']}", "from": m.get("sender"),
                    "account": address, "thread_id": m.get("threadId"), "full": True}
            written += replace_rows(conn, source, m["messageId"], item_rows("email", m["messageTimestamp"], subject, text, meta))
        page = info.get("nextPageToken")
        if not done:
            set_state(conn, source, "jobs_page", page or "")
        if not page:
            break
    if not done and not page:
        set_state(conn, source, "jobs_backfill_done", today())
    return written


def _composio_ready():
    from kb import load_env
    load_env()
    import composio_client as cc
    if not cc.sync_allowed():
        raise NotConfigured("Composio monthly sync budget reached; updates resume next month")
    return bool(os.environ.get("COMPOSIO_API_KEY"))


def github(conn):
    """Your GitHub repositories, read only, for the CV."""
    import composio_client as cc
    if not _composio_ready():
        raise NotConfigured("COMPOSIO_API_KEY is blank in .env")
    written = 0
    for page in range(1, 11):
        data = cc.run("GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER", dict(per_page=100, page=page, sort="pushed"))
        repos = data.get("details") or data.get("repositories") or (data if isinstance(data, list) else [])
        for repo in repos:
            text = f"{repo.get('full_name')}\n{repo.get('description') or ''}\nLanguage: {repo.get('language')}"
            written += replace_rows(conn, "github", str(repo["id"]), [dict(
                kind="project", ts=repo.get("pushed_at") or repo.get("updated_at"), title=repo.get("name"), content=text,
                metadata={"url": repo.get("html_url"), "name": repo.get("name"), "fork": repo.get("fork"),
                          "description": repo.get("description"), "language": repo.get("language"), "private": repo.get("private")})])
        if len(repos) < 100:
            break
    return written


def outlook(conn):
    """Microsoft Graph with Mail.Read only."""
    client_id = os.environ.get("OUTLOOK_CLIENT_ID")
    token_path = os.path.join(SECRETS, "outlook_token.json")
    if not client_id or not os.path.exists(token_path):
        raise NotConfigured("set OUTLOOK_CLIENT_ID in .env, then run setup_auth.py outlook")
    import msal
    import requests

    cache = msal.SerializableTokenCache()
    with open(token_path) as f:
        cache.deserialize(f.read())
    tenant = os.environ.get("OUTLOOK_TENANT", "common")
    app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(["Mail.Read"], account=accounts[0]) if accounts else None
    if not result or "access_token" not in result:
        raise NotConfigured("Outlook login expired, run setup_auth.py outlook")
    if cache.has_state_changed:
        with open(token_path, "w") as f:
            f.write(cache.serialize())
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {result['access_token']}", "Prefer": 'outlook.body-content-type="text"'})

    def pages(url, params=None):
        while url:
            response = session.get(url, params=params, timeout=60)
            response.raise_for_status()
            body = response.json()
            yield from body.get("value", [])
            url, params = body.get("@odata.nextLink"), None

    days = 3 if get_state(conn, "outlook", "backfill_done") else 365
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = "https://graph.microsoft.com/v1.0/me/messages"
    conversations = {m["conversationId"] for m in pages(base, {
        "$filter": f"receivedDateTime ge {since}", "$select": "conversationId", "$top": 200})}
    fields = "subject,from,toRecipients,receivedDateTime,body,webLink"
    new = 0
    for conversation in conversations:
        safe = conversation.replace("'", "''")
        items = list(pages(base, {"$filter": f"conversationId eq '{safe}'", "$select": fields, "$top": 100}))
        messages = [dict(
            date=m["receivedDateTime"],
            sender=(m.get("from") or {}).get("emailAddress", {}).get("address", ""),
            to=", ".join(r["emailAddress"].get("address", "") for r in m.get("toRecipients", [])),
            subject=m.get("subject") or "",
            body=m.get("body", {}).get("content", "")) for m in items]
        if messages:
            url = items[-1].get("webLink") or "https://outlook.office.com/mail/"
            new += _save_thread(conn, "outlook", hashlib.sha1(conversation.encode()).hexdigest(), messages, url, {})
    set_state(conn, "outlook", "backfill_done", today())
    return new


APPLE_EPOCH = 978307200


def moodle(conn):
    """Moodle's web-service API, using only get_* read functions."""
    base, token = os.environ.get("MOODLE_URL", "").rstrip("/"), os.environ.get("MOODLE_TOKEN")
    if not base or not token:
        raise NotConfigured("set MOODLE_URL and MOODLE_TOKEN in .env")
    import requests

    def call(function, **params):
        response = requests.get(f"{base}/webservice/rest/server.php", timeout=60, params={
            "wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params})
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get("exception"):
            raise RuntimeError(f"{function}: {body.get('errorcode')}")
        return body

    def date(stamp):
        return dt.datetime.fromtimestamp(stamp or 0, dt.timezone.utc).isoformat() if stamp else today()

    user = call("core_webservice_get_site_info")["userid"]
    courses = call("core_enrol_get_users_courses", userid=user)
    since = float(get_state(conn, "moodle", "last_modified") or 0)
    newest, new = since, 0
    for course in courses:
        cid, cname = course["id"], course.get("fullname", "course")
        course_url = f"{base}/course/view.php?id={cid}"

        rows = call("mod_assign_get_assignments", **{"courseids[0]": cid}).get("courses", [])
        for block in rows:
            for a in block.get("assignments", []):
                newest = max(newest, a.get("timemodified", 0))
                if a.get("timemodified", 0) <= since:
                    continue
                due = date(a.get("duedate"))
                text = f"Course: {cname}\nDue: {due}\n\n{strip_html(a.get('intro', ''))}"
                new += replace_rows(conn, "moodle", f"assign:{a['id']}", [dict(
                    kind="assignment", ts=due, title=f"{cname}: {a.get('name')} (due {due[:10]})", content=text,
                    metadata={"url": f"{base}/mod/assign/view.php?id={a.get('cmid')}", "due": due, "course": cname})])

        for forum in call("mod_forum_get_forums_by_courses", **{"courseids[0]": cid}):
            body = call("mod_forum_get_forum_discussions", forumid=forum["id"], sortorder=1, perpage=50)
            for d in body.get("discussions", []):
                stamp = d.get("timemodified", 0)
                newest = max(newest, stamp)
                if stamp <= since:
                    continue
                new += replace_rows(conn, "moodle", f"post:{d['discussion']}", [dict(
                    kind="announcement", ts=date(stamp), title=f"{cname}: {d.get('subject')}",
                    content=f"{forum.get('name')}\nFrom: {d.get('userfullname')}\n\n{strip_html(d.get('message', ''))}",
                    metadata={"url": f"{base}/mod/forum/discuss.php?d={d['discussion']}", "course": cname})])

        materials = []
        for section in call("core_course_get_contents", courseid=cid):
            for module in section.get("modules", []):
                files = ", ".join(c.get("filename", "") for c in module.get("contents", []) if c.get("type") == "file")
                materials.append(f"{section.get('name')} / {module.get('name')} ({module.get('modname')}) {files}")
        if materials:
            new += replace_rows(conn, "moodle", f"materials:{cid}", item_rows(
                "material", today(), f"{cname}: study materials", "\n".join(materials), {"url": course_url, "course": cname}))

        report = call("gradereport_user_get_grade_items", courseid=cid, userid=user)
        for grades in report.get("usergrades", []):
            lines = [f"{g.get('itemname') or 'Course total'}: {strip_html(g.get('gradeformatted', '-'))}"
                     f" (range {g.get('grademin')}-{g.get('grademax')})" for g in grades.get("gradeitems", [])]
            new += replace_rows(conn, "moodle", f"grades:{cid}", [dict(
                kind="grade", ts=today(), title=f"{cname}: grades", content="\n".join(lines),
                metadata={"url": f"{base}/grade/report/user/index.php?id={cid}", "course": cname})])
    set_state(conn, "moodle", "last_modified", newest)
    return new


# ---------------------------------------------------------------- Google Calendar
def google_calendar(conn):
    """Index the calendar so it is searchable. Changes are made only through gcal.py."""
    import gcal
    if not _composio_ready():
        raise NotConfigured("COMPOSIO_API_KEY is blank in .env")
    now = dt.datetime.now(gcal.TZ)
    written = 0
    for event in gcal.list_events(now - dt.timedelta(days=30), now + dt.timedelta(days=180)):
        title = event.get("summary") or "(no title)"
        text = f"{title}\nWhen: {gcal.start_of(event)}\nWhere: {event.get('location', '')}\n{event.get('description', '')}"
        written += replace_rows(conn, "gcal", event["id"], [dict(
            kind="event", ts=gcal.start_of(event), title=title, content=strip_html(text),
            metadata={"url": event.get("htmlLink") or f"gcal:{event['id']}", "event_id": event["id"]})])
    return written


# ---------------------------------------------------------------- gym
GYM_BOOK = "DUP_Workout_Log.xlsx"
EPLEY_LIFTS = ("Squat", "Bench", "Deadlift", "OHP")
DAY_ORDER = ["Legs 1", "Push 1", "Pull 1", "Legs 2", "Push 2", "Pull 2"]


def _write_if_changed(path, text):
    if os.path.exists(path):
        with open(path, newline="") as f:
            if f.read() == text:
                return
    with open(path, "w", newline="") as f:
        f.write(text)


def _csv_text(header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def gym(conn):
    """Rebuild aspects/gym/*.csv and progress.md from the live workout log.

    /gym writes the log through aspects/gym/source/coach.py. This only reads it.
    aspects() then indexes the files written here.
    """
    import openpyxl
    folder = os.path.join(ASPECTS, "gym")
    backup = os.path.join(folder, "source", GYM_BOOK)
    if not os.path.exists(backup):
        raise NotConfigured("workout log not found")
    if get_state(conn, "gym", "book_mtime") == str(os.path.getmtime(backup)):
        return 0
    book = openpyxl.load_workbook(backup, read_only=True, data_only=False)
    def table(name):
        return [r for r in list(book[name].iter_rows(min_row=2, values_only=True)) if r[0] is not None]
    sessions, sets, program = table("Sessions"), table("Sets"), table("Program")
    book.close()

    # Volume and 1RM are recalculated here: the workbook stores them as formulas.
    set_rows, best = [], {}
    for r in sets:
        number, date, week, day, exercise, target, set_no, weight, unit, reps = r[:10]
        note = r[12] if len(r) > 12 else None
        is_rm = str(target).endswith("RM")
        estimate = (weight if reps == 1 else round(weight * (1 + reps / 30), 1)) if is_rm else ""
        set_rows.append([date, exercise, target, set_no, weight, unit, reps, weight * reps, estimate, week, day, number, note])
        if is_rm and exercise in EPLEY_LIFTS and estimate > best.get(exercise, (0,))[0]:
            best[exercise] = (estimate, unit, weight, reps, date)
    _write_if_changed(os.path.join(folder, "sets.csv"), _csv_text(
        ["date", "exercise", "target", "set", "weight", "unit", "reps", "volume", "est_1rm", "week", "day", "session", "notes"], set_rows))
    done = [s for s in sessions if s[3] is not None]
    session_rows = []
    for s in sessions:
        own = [r for r in sets if r[0] == s[0]]
        session_rows.append([s[1], s[6], s[5], s[2], s[3], s[4], len(own), sum(r[7] * r[9] for r in own), s[0], s[9] if len(s) > 9 else None])
    _write_if_changed(os.path.join(folder, "sessions.csv"), _csv_text(
        ["date", "day", "week", "start", "end", "minutes", "sets", "volume", "session", "notes"], session_rows))
    _write_if_changed(os.path.join(folder, "program.csv"), _csv_text(
        ["exercise", "prescription", "day", "week", "order", "note"], [[p[3], p[4], p[1], p[0], p[2], p[5]] for p in program]))

    week, day = 1, DAY_ORDER[0]
    if done:
        week, last = done[-1][5], done[-1][6]
        index = DAY_ORDER.index(last) + 1
        week, day = (week, DAY_ORDER[index]) if index < 6 else (week + 1, DAY_ORDER[0])
    lines = ["# Gym progress", "", "Built from the log kept by /gym. Do not edit: it is rebuilt automatically.", "",
             f"Workouts completed: {len(done)} of {len({(p[0], p[1]) for p in program})}",
             f"Sets logged: {len(set_rows)}", f"Total volume: {sum(r[7] for r in set_rows):,.0f}", f"Next workout: Week {week} {day}", "", "## Next workout plan", ""]
    lines += [f"- {p[3]}: {p[4]}" for p in program if p[0] == week and p[1] == day]
    lines += ["", "## Best estimated 1RM", ""]
    lines += [f"- {lift}: {v[0]} {v[1]} (from {v[2]:g} x {v[3]} on {v[4]})" for lift, v in best.items()] or ["- None logged yet"]
    _write_if_changed(os.path.join(folder, "progress.md"), "\n".join(lines) + "\n")
    set_state(conn, "gym", "book_mtime", os.path.getmtime(backup))
    return len(set_rows) + len(program)


# ---------------------------------------------------------------- aspects
def _row_date(row, fallback):
    for key, value in row.items():
        if key and "date" in key.lower() or key and key.lower() == "connected on":
            for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y"):
                try:
                    return dt.datetime.strptime((value or "").strip(), fmt).date().isoformat()
                except ValueError:
                    pass
    return fallback


def aspects(conn):
    """Every folder in aspects/ is one area of your life, and its name is the source.

    Add an aspect by making a new folder. No code changes are needed.
    Reads .csv (one row per record), .md, .txt, .html and .json.
    """
    new = 0
    seen = set()
    for path in sorted(glob.glob(os.path.join(ASPECTS, "*", "**", "*"), recursive=True)):
        ext = os.path.splitext(path)[1].lower()
        if not os.path.isfile(path) or ext not in (".csv", ".md", ".txt", ".html", ".htm", ".json"):
            continue
        rel = os.path.relpath(path, ASPECTS)
        source = rel.split(os.sep)[0]
        seen.add(f"{source}|{rel}")
        mtime = str(os.path.getmtime(path))
        if get_state(conn, "aspects", rel) == mtime:
            continue
        file_date = dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        name = os.path.basename(path)
        with open(path, errors="replace", newline="") as f:
            raw = f.read()
        if ext == ".csv":
            lines = raw.splitlines()
            # LinkedIn exports put notes above the real header
            start = next((i for i, l in enumerate(lines) if l.startswith("First Name")), 0)
            rows = []
            for number, row in enumerate(csv.DictReader(lines[start:]), start + 2):
                values = {k: v for k, v in row.items() if k and v}
                if values:
                    rows.append(dict(
                        kind="record", ts=_row_date(row, file_date),
                        title=" | ".join(list(values.values())[:3])[:120],
                        content="; ".join(f"{k}: {v}" for k, v in values.items()),
                        metadata={"path": f"{path}#L{number}", **({"url": row["URL"]} if row.get("URL") else {})}))
        else:
            text = strip_html(raw) if ext in (".html", ".htm") else raw
            rows = item_rows("note", file_date, f"{source}: {name}", text, {"path": path})
        new += replace_rows(conn, source, rel, rows)
        set_state(conn, "aspects", rel, mtime)
    for row in conn.execute("SELECT key FROM ingest_state WHERE source='aspects'").fetchall():  # file was deleted
        rel = row["key"]
        if f"{rel.split(os.sep)[0]}|{rel}" not in seen:
            replace_rows(conn, rel.split(os.sep)[0], rel, [])
            with conn:
                conn.execute("DELETE FROM ingest_state WHERE source='aspects' AND key=?", (rel,))
    return new


# ---------------------------------------------------------------- projects
def projects(conn):
    """Track your projects (for the CV) from session folders plus PROJECT_DIRS."""
    dirs = {}
    for row in conn.execute("SELECT json_extract(metadata,'$.cwd') cwd, COUNT(DISTINCT source_id) n "
                            "FROM docs WHERE source='claude_code' GROUP BY cwd"):
        if row["cwd"]:
            dirs[row["cwd"]] = row["n"]
    for extra in filter(None, os.environ.get("PROJECT_DIRS", "").split(",")):
        dirs.setdefault(os.path.expanduser(extra.strip()), 0)
    new = 0
    for folder, sessions in dirs.items():
        if "scratch-workspaces" in folder or not os.path.isdir(folder) or os.path.realpath(folder) == HOME:
            continue
        files, newest = {}, 0
        for base, subdirs, names in os.walk(folder):
            subdirs[:] = [d for d in subdirs if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__")]
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext:
                    files[ext] = files.get(ext, 0) + 1
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(base, n)))
                except OSError:
                    pass
        stamp = f"{newest}:{sessions}"
        if get_state(conn, "projects", folder) == stamp:
            continue
        readme = ""
        for candidate in ("README.md", "README.txt", "readme.md"):
            target = os.path.join(folder, candidate)
            if os.path.exists(target):
                with open(target, errors="replace") as f:
                    readme = f.read(3000)
                break
        commits = ""
        if os.path.isdir(os.path.join(folder, ".git")):
            done = subprocess.run(["git", "-C", folder, "log", "-20", "--format=%ad %s", "--date=short"],
                                  capture_output=True, text=True, timeout=30)
            commits = done.stdout
        top = sorted(files, key=files.get, reverse=True)[:8]
        name = os.path.basename(folder)
        content = (f"Project: {name}\nFolder: {folder}\nClaude Code sessions: {sessions}\n"
                   f"File types: {', '.join(top)}\n\nREADME:\n{readme}\n\nRecent commits:\n{commits}")
        ts = dt.date.fromtimestamp(newest).isoformat() if newest else today()
        new += replace_rows(conn, "projects", folder, [dict(
            kind="project", ts=ts, title=f"Project: {name}", content=content,
            metadata={"path": folder, "name": name, "tech": top, "has_readme": bool(readme)})])
        set_state(conn, "projects", folder, stamp)
    return new


# ---------------------------------------------------------------- job tracker
_APPLIED = re.compile(r"(?i)application (was )?(sent|submitted|received)|thanks? (you )?for applying|you applied|your application (to|for)")
_REJECTED = re.compile(r"(?i)unfortunately|not (be )?moving forward|regret to inform|other candidates|not been selected")
_INTERVIEW = re.compile(r"(?i)\binterview\b|phone screen|schedule a (call|chat)|next steps? in (the|our) (process|hiring)|assessment invitation")
_JOB_WORD = re.compile(r"(?i)\bapplication\b|\bapplied\b|\bapplying\b|\bcandidate\b|\bposition\b|\brecruit")


def job_events(conn):
    """Find applications and replies in mail, and rebuild output/job_tracker.md."""
    last = int(get_state(conn, "job_events", "last_id") or 0)
    rows = conn.execute(
        """SELECT * FROM docs WHERE id > ? AND (source LIKE 'gmail%' OR source = 'outlook')
           AND (kind='email' OR (kind='raw' AND json_extract(metadata,'$.part')=1)) ORDER BY id""", (last,)).fetchall()
    new = 0
    for row in rows:
        last = max(last, row["id"])
        text = f"{row['title']}\n{row['content'][:3000]}"
        if not _JOB_WORD.search(text):
            continue
        status = ("rejected" if _REJECTED.search(text) else "interview" if _INTERVIEW.search(text)
                  else "applied" if _APPLIED.search(text) else None)
        if status:
            meta = json.loads(row["metadata"])
            new += replace_rows(conn, "job_tracker", f"{row['source']}:{row['source_id']}", [dict(
                kind="job_event", ts=row["ts"], title=f"{status}: {row['title']}",
                content=f"Status: {status}\nFrom: {meta.get('from')}\nSubject: {row['title']}",
                metadata={"url": meta.get("url"), "status": status, "from": meta.get("from")})])
    set_state(conn, "job_events", "last_id", last)
    events = conn.execute("SELECT * FROM docs WHERE source='job_tracker' ORDER BY ts DESC").fetchall()
    os.makedirs(OUTPUT, exist_ok=True)
    with open(os.path.join(OUTPUT, "job_tracker.md"), "w") as f:
        f.write(f"# Job tracker (from your email, updated {today()})\n\n")
        f.write("Detected by keyword, so check the original before relying on a status. "
                "Your own entries live in aspects/jobs/applications.csv.\n\n")
        f.write("| Date | Status | Subject | Original |\n|---|---|---|---|\n")
        for e in events:
            meta = json.loads(e["metadata"])
            subject = e["title"].split(": ", 1)[-1].replace("|", "/")[:90]
            f.write(f"| {e['ts'][:10]} | {meta.get('status')} | {subject} | [open]({meta.get('url')}) |\n")
    return new


# ---------------------------------------------------------------- the KB itself
def self_docs(conn):
    """The second brain also knows about itself: its README and a daily snapshot."""
    path = os.path.join(ROOT, "README.md")
    new = 0
    if os.path.exists(path):
        with open(path) as f:
            new += replace_rows(conn, "self", "README", item_rows(
                "note", dt.date.fromtimestamp(os.path.getmtime(path)).isoformat(), "Second brain: how it works", f.read(), {"path": path}))
    lines = [f"{r['source']}: {r['n']} rows" for r in conn.execute(
        "SELECT source, COUNT(*) n FROM docs WHERE source != 'self' GROUP BY source ORDER BY n DESC")]
    new += replace_rows(conn, "self", f"snapshot:{today()}", [dict(
        kind="snapshot", ts=today(), title=f"Second brain snapshot {today()}", content="\n".join(lines),
        metadata={"path": os.path.join(ROOT, "kb.sqlite")})])
    return new


CONNECTORS = [
    ("claude_code", claude_code), ("gmail", gmail), ("github", github), ("outlook", outlook),
    ("moodle", moodle), ("gcal", google_calendar), ("gym", gym), ("aspects", aspects), ("projects", projects), ("job_events", job_events), ("self", self_docs),
]
