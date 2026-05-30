#!/usr/bin/env python3
"""
tail_alerts.py

Tails eve.json written by Suricata and writes one JSON file per alerting
flow into FLOW_DIR:

    FLOW_DIR/<timestamp>_<flow_id>.json

Each file contains a JSON array of all events sharing that flow_id.
The timestamp prefix is the ISO8601 time of the first event seen for
that flow, making files sortable by occurrence time and avoiding
collisions across Suricata restarts.

Strategy:
- Buffer all events per flow_id in memory
- When an alert is seen, mark that flow_id as alerting
- When a flow event (end-of-flow) arrives for an alerting flow_id,
  write the file and clean up
- Expire flows older than FLOW_TTL seconds (covers flows that never
  produce a closing flow event)

Rotation (when eve.json exceeds MAX_SIZE):
- Rename eve.json -> eve.json.old  (Suricata keeps writing via open fd)
- Send SIGHUP                      (Suricata creates fresh eve.json)
- Main readline() loop drains old fd to true EOF
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

EVE_PATH      = os.getenv("EVE_PATH",   "/var/log/suricata/eve.json")
FLOW_DIR      = os.getenv("FLOW_DIR",   "/var/log/suricata/flows")
MAX_SIZE      = int(os.getenv("MAX_SIZE",      str(100 * 1024 * 1024)))  # 100MB
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.1"))                 # seconds
FLOW_TTL      = float(os.getenv("FLOW_TTL",      "120"))                 # seconds

EVE_OLD_PATH  = EVE_PATH + ".old"


# ── flow buffer ───────────────────────────────────────────────────────────────

class FlowBuffer:
    """
    Accumulates events per flow_id.
    Tracks which flow_ids have produced an alert.
    Flushes all events for a flow when the closing flow event arrives.
    Expires stale flows that never close cleanly.
    """

    def __init__(self):
        self.events:     dict[int, list[dict]] = defaultdict(list)
        self.alerted:    set[int]              = set()
        self.first_seen: dict[int, float]      = {}   # monotonic time
        self.first_ts:   dict[int, str]        = {}   # ISO8601 from event

    def add(self, event: dict) -> list[dict] | None:
        """
        Add an event. Returns list of events to flush, or None.
        """
        flow_id    = event.get("flow_id")
        event_type = event.get("event_type")

        if flow_id is None:
            return None

        if flow_id not in self.first_seen:
            self.first_seen[flow_id] = time.monotonic()
            self.first_ts[flow_id]   = event.get("timestamp", "unknown")

        self.events[flow_id].append(event)

        if event_type == "alert":
            self.alerted.add(flow_id)

        if event_type == "flow" and flow_id in self.alerted:
            return self._flush(flow_id)

        return None

    def expire(self) -> list[tuple[str, list[dict]]]:
        """
        Return and remove all event groups exceeding FLOW_TTL that had an alert.
        Returns list of (first_ts, events) tuples.
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

    def _flush(self, flow_id: int) -> tuple[str, list[dict]]:
        first_ts = self.first_ts.pop(flow_id, "unknown")
        events   = self.events.pop(flow_id, [])
        self.alerted.discard(flow_id)
        self.first_seen.pop(flow_id, None)
        return (first_ts, events)

    def _discard(self, flow_id: int) -> None:
        self.events.pop(flow_id, None)
        self.alerted.discard(flow_id)
        self.first_seen.pop(flow_id, None)
        self.first_ts.pop(flow_id, None)


# ── helpers ───────────────────────────────────────────────────────────────────

def wait_for_file(path: str) -> None:
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
    print(f"Removing {EVE_OLD_PATH}", flush=True)
    try:
        os.remove(EVE_OLD_PATH)
    except FileNotFoundError:
        pass


def write_flow(first_ts: str, events: list[dict]) -> None:
    """Write all events for one alerting flow to FLOW_DIR/<timestamp>_<flow_id>.json"""
    flow_id  = events[0].get("flow_id", "unknown")

    # Normalise timestamp to a filename-safe string: 2026-05-28T19:21:36.252199+0000
    # -> 20260528T192136
    safe_ts  = first_ts[:19].replace("-", "").replace(":", "")

    filename = f"{safe_ts}_{flow_id}.json"
    path     = os.path.join(FLOW_DIR, filename)

    with open(path, "w") as f:
        json.dump(events, f, indent=2)

    print(f"Written {filename} ({len(events)} events)", flush=True)


# ── main tail loop ────────────────────────────────────────────────────────────

def tail_eve(eve_path: str, buf: FlowBuffer) -> None:
    inode              = get_inode(eve_path)
    rotation_signalled = False
    last_expire        = time.monotonic()

    print(f"Opening {eve_path} (inode {inode})", flush=True)

    with open(eve_path) as f:
        while True:
            line = f.readline()

            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                result = buf.add(event)
                if result:
                    first_ts, events = result
                    write_flow(first_ts, events)

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

                # Periodic TTL expiry
                now = time.monotonic()
                if now - last_expire > FLOW_TTL / 2:
                    for first_ts, events in buf.expire():
                        print(
                            f"Expiring flow_id {events[0].get('flow_id')} "
                            f"({len(events)} events)",
                            flush=True,
                        )
                        write_flow(first_ts, events)
                    last_expire = now

                # Rotation check
                if not rotation_signalled and os.path.getsize(eve_path) > MAX_SIZE:
                    if rotate_eve():
                        rotation_signalled = True

                time.sleep(POLL_INTERVAL)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    os.makedirs(FLOW_DIR, exist_ok=True)
    print(
        f"Starting eve tailer\n"
        f"  EVE_PATH={EVE_PATH}\n"
        f"  FLOW_DIR={FLOW_DIR}\n"
        f"  MAX_SIZE={MAX_SIZE / 1024 / 1024:.0f}MB\n"
        f"  POLL_INTERVAL={POLL_INTERVAL}s\n"
        f"  FLOW_TTL={FLOW_TTL}s",
        flush=True,
    )

    buf = FlowBuffer()  # survives across inode changes

    while True:
        wait_for_file(EVE_PATH)
        tail_eve(EVE_PATH, buf)


if __name__ == "__main__":
    main()
