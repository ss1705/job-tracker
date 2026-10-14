"""Background auto-sync: while the server runs, re-sync Gmail every AUTO_SYNC_MINUTES.

Each run looks back AUTO_SYNC_LOOKBACK_DAYS (emails already seen are skipped, so this is cheap) and is
skipped when Gmail isn't signed in. After a failed run (e.g. Groq quota) it waits ERROR_BACKOFF_MINUTES.
"""
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

from . import config, db, digest, gmail, sync

ERROR_BACKOFF_MINUTES = 60
STARTUP_DELAY_SECONDS = 20
CHECK_EVERY_SECONDS = 60


def is_due(last: dict | None, now: datetime, interval_min: int) -> bool:
    if interval_min <= 0:
        return False
    if not last:
        return True
    wait = max(interval_min, ERROR_BACKOFF_MINUTES) if last.get("error") else interval_min
    at = datetime.strptime(last["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - at >= timedelta(minutes=wait)


def _last_sync() -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    return json.loads(row[0]) if row else None


def _record_sync_problem(message: str):
    """Store a failed background sync as the latest sync result, so the UI shows why nothing is updating."""
    with db.session() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)", (json.dumps({
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "since": date.today().isoformat(), "added": 0, "updated": 0,
            "ignored": 0, "failed": 0, "needs_review": 0, "processed": 0, "total": 0, "error": message}),))


def _loop():
    time.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            if config.AUTO_SYNC_MINUTES > 0 and not sync.state.running and is_due(_last_sync(), datetime.now(timezone.utc), config.AUTO_SYNC_MINUTES):
                gmail.authorize()  # raises AuthRequired when not signed in -> skip quietly
                since = date.today() - timedelta(days=config.AUTO_SYNC_LOOKBACK_DAYS)
                print(f"[auto-sync] syncing since {since}")
                sync.run_sync(since)
        except gmail.AuthRequired as e:
            _record_sync_problem(str(e))                # shown in red on the page; also backs off instead of retrying every minute
        except Exception as e:
            print(f"[auto-sync] error: {type(e).__name__}: {e}")
        try:
            if config.DIGEST_ENABLED and digest.is_due(digest.local_now(), digest.last_digest(), config.DIGEST_HOUR):
                r = digest.send_now()
                print(f"[digest] {'sent to ' + r.get('to', '') if r['sent'] else 'skipped (nothing to report)' if r['skipped'] else 'not sent: ' + r['error']}")
        except Exception as e:
            print(f"[digest] error: {type(e).__name__}: {e}")
        time.sleep(CHECK_EVERY_SECONDS)


_started = False


def start():
    global _started
    if _started or (config.AUTO_SYNC_MINUTES <= 0 and not config.DIGEST_ENABLED):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="auto-sync").start()
    print(f"[auto-sync] enabled: every {config.AUTO_SYNC_MINUTES} min, looking back {config.AUTO_SYNC_LOOKBACK_DAYS} days")
