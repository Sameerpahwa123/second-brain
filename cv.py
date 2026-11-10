#!/usr/bin/env python3
"""Rebuild output/CV.md from aspects/cv/profile.md plus your tracked projects.

Only facts from your profile and your project folders appear. Nothing is invented.
Runs after each ingest. Add one line to a project's README to improve its entry.
"""
import json
import os
import re

import kb


def first_paragraph(content):
    readme = content.split("README:\n", 1)[-1].split("\n\nRecent commits:", 1)[0]
    for block in re.split(r"\n\s*\n", readme):
        block = block.strip()
        if block and not block.startswith(("#", "!", "[", "<", "```")):
            return " ".join(block.split())[:400]
    return ""


def extra_projects(conn, seen):
    """GitHub repositories with a description, then projects and hackathons the Jobs agent recorded."""
    lines = []
    for row in conn.execute("SELECT * FROM docs WHERE source='career' AND kind IN ('project','hackathon') "
                            "AND json_extract(metadata,'$.status')='done' ORDER BY ts DESC"):
        seen.add(row["title"].lower())
        lines += [f"### {row['title']} ({row['ts'][:7]})", row["content"], ""]
    for row in conn.execute("SELECT * FROM docs WHERE source='github' ORDER BY ts DESC"):
        meta = json.loads(row["metadata"])
        if meta.get("fork") or not meta.get("description") or row["title"].lower() in seen:
            continue
        lines += [f"### {row['title']} (last active {row['ts'][:7]})", meta["description"], f"*{meta.get('language') or ''}*", ""]
    return lines


def build(conn):
    profile_path = os.path.join(kb.ROOT, "aspects", "cv", "profile.md")
    profile = open(profile_path).read().strip() if os.path.exists(profile_path) else "# Your Name"
    rows = conn.execute("SELECT * FROM docs WHERE source='projects' ORDER BY ts DESC").fetchall()
    lines = [profile, "", "## Projects", ""]
    need_description = []
    for row in rows:
        meta = json.loads(row["metadata"])
        text = first_paragraph(row["content"])
        if not text:
            need_description.append(meta["name"])
            continue
        tech = ", ".join(t.lstrip(".") for t in meta.get("tech", [])[:5])
        lines += [f"### {meta['name']} (last active {row['ts'][:7]})", text, f"*File types: {tech}*", ""]
    lines += extra_projects(conn, {json.loads(r["metadata"])["name"].lower() for r in rows})
    if need_description:
        lines += [f"<!-- Not on the CV yet because they have no README description: {', '.join(need_description)} -->", ""]
    os.makedirs(os.path.join(kb.ROOT, "output"), exist_ok=True)
    with open(os.path.join(kb.ROOT, "output", "CV.md"), "w") as f:
        f.write("\n".join(lines))
    return f"rebuilt output/CV.md: {len(rows) - len(need_description)} projects, {len(need_description)} need a README"


if __name__ == "__main__":
    print(build(kb.connect()))
