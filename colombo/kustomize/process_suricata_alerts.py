#!/usr/bin/env python3
"""
tail_alerts.py

Tails eve.json written by Suricata and writes to ALERT_PATH all events
that share a flow_id with an alert event.

Strategy:
- Buffer all events per flow_id in memory
- When an alert is seen, mark that flow_id as alerting
- When a flow event (end-of-flow) arrives for an alerting flow_id,
  flush all buffered events for that flow and clean up
- Expire flows older than FLOW_TTL seconds to cap memory usage
  (covers flows that never produce a closing flow event)

Rotation (when eve.json exceeds MAX_SIZE):
- Rename eve.json -> eve.json.old  (Suricata keeps writing via open fd)
- Send SIGHUP                      (Suricata creates fresh eve.json)
- Main readline() loop drains the old fd to true EOF
- remove_old() deletes eve.json.old
- main() loop reopens the new eve.json

Requires shareProcessNamespace: true in the pod spec.
"""

import os
import json
import time
import signal
import subprocess
from collections import defaultdict

EVE_PATH      = os.getenv("EVE_PATH",      "/var/log/suricata/eve.json")
ALERT_PATH    = os.getenv("ALERT_PATH",    "/var/log/suricata/alerts.json")
MAX_SIZE      = int(os.getenv("MAX_SIZE",  str(100 * 1024 * 1024)))  # 100MB
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.1"))             # seconds
FLOW_TTL      = float(os.getenv("FLOW_TTL", "120"))                  # seconds


EVE_OLD_PATH = EVE_PATH + ".old"


# ── flow buffer ───────────────────────────────────────────────────────────────

class FlowBuffer:
    """
    Accumulates events per flow_id.
    Tracks which flow_ids have produced an alert.
    Flushes all events for a flow when the closing flow event arrives.
    Expires stale flows that never close cleanly.
    """

    def __init__(self):
        self.events:    dict[int, list[dict]] = defaultdict(list)  # flow_id -> events
        self.alerted:   set[int]              = set()              # flow_ids with alerts
        self.first_seen:dict[int, float]      = {}                 # flow_id -> time.monotonic()

    def add(self, event: dict) -> list[dict] | None:
        """
        Add an event to the buffer.
        Returns the list of events to flush, or None if nothing to flush yet.
        """
        flow_id      = event.get("flow_id")
        event_type   = event.get("event_type")

        # Events without a flow_id (e.g. stats) are ignored
        if flow_id is None:
            return None

        # Record first-seen time for TTL expiry
        if flow_id not in self.first_seen:
            self.first_seen[flow_id] = time.monotonic()

        self.events[flow_id].append(event)

        if event_type == "alert":
            self.alerted.add(flow_id)

        # flow event = end of flow → flush if this flow produced an alert
        if event_type == "flow" and flow_id in self.alerted:
            return self._flush(flow_id)

        return None

    def expire(self) -> list[list[dict]]:
        """
        Return and remove all event groups whose first-seen age exceeds FLOW_TTL.
        Only flushes flows that produced at least one alert.
        """
        now     = time.monotonic()
        expired = []
        for flow_id, ts in list(self.first_seen.items()):
            if now - ts > FLOW_TTL:
                if flow_id in self.alerted:
                    expired.append(self._flush(flow_id))
                else:
                    self._discard(flow_id)
        return expired

    def _flush(self, flow_id: int) -> list[dict]:
        events = self.events.pop(flow_id, [])
        self.alerted.discard(flow_id)
        self.first_seen.pop(flow_id, None)
        return events

    def _discard(self, flow_id: int) -> None:
        self.events.pop(flow_id, None)
        self.alerted.discard(flow_id)
        self.first_seen.pop(flow_id, None)


# ── helpers ───────────────────────────────────────────────────────────────────

def wait_for_file(path: str) -> None:
    """Block until path exists."""
    while not os.path.exists(path):
        print(f"Waiting for {path} to appear...", flush=True)
        time.sleep(1)


def get_inode(path: str) -> int | None:
    try:
        return os.stat(path).st_ino
    except FileNotFoundError:
        return None


def get_suricata_pid() -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "suricata"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return int(result.stdout.strip().splitlines()[0])
    except (ValueError, FileNotFoundError) as e:
        print(f"pgrep failed: {e}", flush=True)
    return None


def rotate_eve() -> bool:
    """
    Move eve.json -> eve.json.old then send SIGHUP.
    Suricata keeps writing to the old inode via its open fd,
    then creates a fresh eve.json after SIGHUP.
    Returns True if rotation was initiated.
    """
    pid = get_suricata_pid()
    if not pid:
        print("Could not find suricata pid — skipping rotation", flush=True)
        return False
    print(
        f"Size threshold reached — moving {EVE_PATH} -> {EVE_OLD_PATH} "
        f"then sending SIGHUP to suricata (pid {pid})",
        flush=True,
    )
    os.rename(EVE_PATH, EVE_OLD_PATH)
    os.kill(pid, signal.SIGHUP)
    return True


def remove_old() -> None:
    """
    Delete eve.json.old. Called only after the main readline() loop has
    reached true EOF on the old file handle — all data already processed.
    """
    print(f"Removing {EVE_OLD_PATH}", flush=True)
    try:
        os.remove(EVE_OLD_PATH)
    except FileNotFoundError:
        pass


def flush_events(events: list[dict], out) -> None:
    """Write a group of correlated events as a JSON array line."""
    out.write(json.dumps(events) + "\n")
    out.flush()


# ── main tail loop ────────────────────────────────────────────────────────────

def tail_eve(eve_path: str, alert_path: str, buf: FlowBuffer) -> None:
    """
    Read eve_path from current position, buffer events by flow_id,
    flush complete alerting flows to alert_path.

    Returns when the inode changes (rotation or Suricata restart)
    or the file disappears unexpectedly.

    The FlowBuffer is passed in so partially-buffered flows survive
    across inode changes (e.g. a flow spanning a rotation boundary).
    """
    inode              = get_inode(eve_path)
    rotation_signalled = False
    last_expire        = time.monotonic()

    print(f"Opening {eve_path} (inode {inode})", flush=True)

    with open(eve_path) as f, open(alert_path, "a") as out:
        while True:
            line = f.readline()

            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # skip corrupt/partial lines

                to_flush = buf.add(event)
                if to_flush:
                    print(
                        f"Flushing {len(to_flush)} events for "
                        f"flow_id {event.get('flow_id')}",
                        flush=True,
                    )
                    flush_events(to_flush, out)

            else:
                # EOF — no new data yet
                current_inode = get_inode(eve_path)

                if current_inode is None and not rotation_signalled:
                    print("eve.json disappeared unexpectedly — waiting...", flush=True)
                    return

                if current_inode is not None and current_inode != inode:
                    if rotation_signalled:
                        remove_old()
                    else:
                        print(
                            f"Inode changed unexpectedly ({inode} -> {current_inode})"
                            " — Suricata restarted, reopening...",
                            flush=True,
                        )
                    return

                # Periodic TTL expiry of flows that never produced a flow event
                now = time.monotonic()
                if now - last_expire > FLOW_TTL / 2:
                    for events in buf.expire():
                        print(
                            f"Expiring flow_id {events[0].get('flow_id')} "
                            f"({len(events)} events)",
                            flush=True,
                        )
                        flush_events(events, out)
                    last_expire = now

                # Rotation check
                if not rotation_signalled and os.path.getsize(eve_path) > MAX_SIZE:
                    if rotate_eve():
                        rotation_signalled = True

                time.sleep(POLL_INTERVAL)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(
        f"Starting eve tailer\n"
        f"  EVE_PATH={EVE_PATH}\n"
        f"  ALERT_PATH={ALERT_PATH}\n"
        f"  MAX_SIZE={MAX_SIZE / 1024 / 1024:.0f}MB\n"
        f"  POLL_INTERVAL={POLL_INTERVAL}s\n"
        f"  FLOW_TTL={FLOW_TTL}s",
        flush=True,
    )

    buf = FlowBuffer()  # survives across inode changes

    while True:
        wait_for_file(EVE_PATH)
        tail_eve(EVE_PATH, ALERT_PATH, buf)


if __name__ == "__main__":
    main()
