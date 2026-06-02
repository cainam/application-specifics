#!/usr/bin/env python3
"""
api.py

FastAPI service exposing Suricata flow data to the web server.

Endpoints:
  GET /flows              — list of all flows, newest first, with summary
  GET /flows/<flow_id>    — full flow events + enrichment (enrichment null if not ready)

Environment variables:
  FLOWS_DIR      directory containing flow and enrichment files
                 (default: /var/log/suricata/flows_enriched)
  HOST           bind host (default: 0.0.0.0)
  PORT           bind port (default: 8000)
"""

import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

flows_dir_env = os.getenv("FLOWS_DIR")
if not flows_dir_env:
    raise KeyError("Environment variable 'FLOWS_DIR' is not set.")
FLOWS_DIR = Path(flows_dir_env)

app = FastAPI(title="Suricata Flow API")

# Filename pattern: 20260528T192136_37918685728178.json
FLOW_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)\.json$")


# ── helpers ───────────────────────────────────────────────────────────────────

def parse_filename(filename: str) -> tuple[str, str] | None:
    """Return (timestamp_str, flow_id) or None if not a flow file."""
    m = FLOW_RE.match(filename)
    if not m:
        return None
    return m.group(1), m.group(2)


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def flow_summary(timestamp_str: str, flow_id: str, events: list, enrichment: dict | None) -> dict:
    """Build the summary dict returned by GET /flows."""

    # Extract alert event for signature info
    alert = next((e for e in events if e.get("event_type") == "alert"), {})
    flow  = next((e for e in events if e.get("event_type") == "flow"),  {})

    # Parse filename timestamp into ISO8601
    ts = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()

    return {
        "flow_id":   flow_id,
        "timestamp": ts,
        "src_ip":    alert.get("src_ip"),
        "dest_ip":   alert.get("dest_ip"),
        "dest_port": alert.get("dest_port"),
        "proto":     alert.get("proto"),
        "signature": alert.get("alert", {}).get("signature"),
        "severity":  alert.get("alert", {}).get("severity"),
        "category":  alert.get("alert", {}).get("category"),
        "app_proto": alert.get("app_proto"),
        "bytes_toserver": flow.get("flow", {}).get("bytes_toserver"),
        "bytes_toclient": flow.get("flow", {}).get("bytes_toclient"),
        "verdict":   enrichment.get("verdict") if enrichment else None,
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/flows")
def list_flows():
    """List all flows, newest first, with summary info."""
    results = []

    for path in sorted(FLOWS_DIR.glob("*.json"), reverse=True):
        parsed = parse_filename(path.name)
        if not parsed:
            continue  # skip enrichment files and anything unexpected
        timestamp_str, flow_id = parsed

        events = read_json(path)
        if not isinstance(events, list):
            continue

        enrichment_path = path.with_suffix("").with_suffix(".enrichment.json")
        # with_suffix replaces last suffix, so do it in two steps:
        enrichment_path = path.parent / (path.stem + ".enrichment.json")
        enrichment      = read_json(enrichment_path) if enrichment_path.exists() else None

        results.append(flow_summary(timestamp_str, flow_id, events, enrichment))

    return JSONResponse(content=results)


@app.get("/flows/{flow_id}")
def get_flow(flow_id: str):
    """Return full flow events and enrichment for a given flow_id."""

    # Find the flow file — there may be multiple files with the same flow_id
    # (unlikely but possible across restarts); return the newest
    matches = sorted(FLOWS_DIR.glob(f"*_{flow_id}.json"), reverse=True)
    matches = [p for p in matches if parse_filename(p.name)]

    if not matches:
        raise HTTPException(status_code=404, detail=f"Flow {flow_id} not found")

    flow_path = matches[0]
    events    = read_json(flow_path)

    if not isinstance(events, list):
        raise HTTPException(status_code=500, detail="Flow file is corrupt")

    enrichment_path = flow_path.parent / (flow_path.stem + ".enrichment.json")
    enrichment      = read_json(enrichment_path) if enrichment_path.exists() else None

    return JSONResponse(content={
        "flow_id":    flow_id,
        "flow":       events,
        "enrichment": enrichment,   # null if enrichment not ready yet
    })


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
        access_log=True
    )
