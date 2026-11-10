#!/usr/bin/env python3
"""Run every connector. One failure is logged and the others keep going.

Log: logs/ingest.log
"""
import datetime as dt
import fcntl
import logging
import logging.handlers
import os
import sys
import traceback

import kb
from connectors import CONNECTORS, NotConfigured

os.makedirs(os.path.join(kb.ROOT, "logs"), exist_ok=True)
LOG_PATH = os.path.join(kb.ROOT, "logs", "ingest.log")
log = logging.getLogger("ingest")
log.setLevel(logging.INFO)
_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.addHandler(logging.StreamHandler(sys.stdout))


def stamp():
    return dt.datetime.now().isoformat(timespec="seconds")


def main():
    lock = open(os.path.join(kb.ROOT, "logs", ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Another ingest is already running; exiting.")
        return
    kb.load_env()
    conn = kb.connect()
    log.info("=== ingest start ===")
    failures = 0
    for name, connector in CONNECTORS:
        try:
            count = connector(conn)
            status = f"ok, {count} rows written ({stamp()})"
        except NotConfigured as reason:
            status = f"skipped: {reason} ({stamp()})"
        except Exception as error:  # one broken source must not stop the rest
            failures += 1
            status = f"ERROR {type(error).__name__}: {kb.mask(str(error))[:300]} ({stamp()})"
            log.error(kb.mask(traceback.format_exc()))
        kb.set_state(conn, "_status", name, status)
        log.info("%-12s %s", name, status)

    if os.environ.get("SUMMARIZE_AUTO") == "1" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import summarize
            summarize.run(float(os.environ.get("SUMMARIZE_MAX_USD", "0.25")), log.info)
        except Exception:
            failures += 1
            log.error(kb.mask(traceback.format_exc()))
    try:
        import cv
        log.info("cv           %s", cv.build(conn))
    except Exception:
        failures += 1
        log.error(kb.mask(traceback.format_exc()))
    try:
        import brain
        log.info("reminders    %s", brain.send_contact_reminders(conn))
    except Exception as error:
        failures += 1
        log.error("reminders failed: %s", kb.mask(str(error))[:300])
    kb.set_state(conn, "_meta", "last_ingest", stamp())
    log.info("=== ingest done, %d failures ===", failures)


if __name__ == "__main__":
    main()
