#!/usr/bin/env python3
"""
enrich_flows.py

Enriches a single Suricata flow JSON file with threat intelligence.

Usage:
    python enrich_flows.py <flow_file> <enrichment_output_file>

Sources:
  - AbuseIPDB  — IP reputation (requires ABUSEIPDB_KEY env var)
  - URLhaus    — domain/URL known-bad (no API key required)
  - ja3er.com  — JA3 fingerprint lookup (no API key required)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

ABUSEIPDB_KEY = os.getenv("ABUSEIPDB_KEY", "")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_get(url: str, headers: dict = {}, timeout: int = 10) -> dict | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  GET {url} failed: {e}", flush=True)
        return None


def http_post(url: str, data: dict, headers: dict = {}, timeout: int = 10) -> dict | None:
    body = urllib.parse.urlencode(data).encode()
    req  = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  POST {url} failed: {e}", flush=True)
        return None


# ── threat intel lookups ──────────────────────────────────────────────────────

def lookup_abuseipdb(ip: str) -> dict:
    if not ABUSEIPDB_KEY:
        return {"error": "ABUSEIPDB_KEY not set"}
    result = http_get(
        f"https://api.abuseipdb.com/api/v2/check?ipAddress={ip}&maxAgeInDays=90",
        headers={"Key": ABUSEIPDB_KEY, "Accept": "application/json"},
    )
    if not result:
        return {"error": "request failed"}
    d = result.get("data", {})
    return {
        "abuse_confidence_score": d.get("abuseConfidenceScore"),
        "total_reports":          d.get("totalReports"),
        "last_reported_at":       d.get("lastReportedAt"),
        "isp":                    d.get("isp"),
        "country_code":           d.get("countryCode"),
        "domain":                 d.get("domain"),
        "is_tor":                 d.get("isTor"),
    }


def lookup_urlhaus(domain: str) -> dict:
    result = http_post(
        "https://urlhaus-api.abuse.ch/v1/host/",
        data={"host": domain},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not result:
        return {"error": "request failed"}
    return {
        "query_status": result.get("query_status"),
        "blacklists":   result.get("blacklists"),
        "urls_count":   len(result.get("urls", [])),
        "threat":       result.get("urls", [{}])[0].get("threat") if result.get("urls") else None,
    }


def lookup_ja3(ja3_hash: str) -> dict:
    result = http_get(f"https://ja3er.com/search/{ja3_hash}")
    if not result:
        return {"error": "request failed"}
    if isinstance(result, list) and result:
        return {
            "matches": [
                {"user_agent": r.get("User-Agent"), "count": r.get("Count")}
                for r in result[:5]
            ]
        }
    return {"matches": []}


# ── IOC extraction ────────────────────────────────────────────────────────────

def extract_iocs(events: list[dict]) -> dict:
    private_prefixes = ("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.2", "127.", "::1")
    ips     = set()
    domains = set()
    ja3s    = set()

    for event in events:
        for field in ("src_ip", "dest_ip"):
            ip = event.get(field, "")
            if ip and not any(ip.startswith(p) for p in private_prefixes):
                ips.add(ip)

        dns = event.get("dns", {})
        if isinstance(dns, dict) and dns.get("rrname"):
            domains.add(dns["rrname"].rstrip("."))

        tls = event.get("tls", {})
        if isinstance(tls, dict):
            if tls.get("sni"):
                domains.add(tls["sni"])
            if tls.get("ja3", {}).get("hash"):
                ja3s.add(tls["ja3"]["hash"])

        http = event.get("http", {})
        if isinstance(http, dict) and http.get("hostname"):
            domains.add(http["hostname"])

    return {
        "ips":     sorted(ips),
        "domains": sorted(domains),
        "ja3s":    sorted(ja3s),
    }


# ── verdict ───────────────────────────────────────────────────────────────────

def make_verdict(enrichment: dict) -> dict:
    reasons = []
    level   = "CLEAN"

    for ip, rep in enrichment["ip_reputation"].items():
        score = rep.get("abuse_confidence_score", 0) or 0
        if score >= 50:
            level = "MALICIOUS"
            reasons.append(f"{ip} has AbuseIPDB score {score}%")
        elif score >= 10:
            if level != "MALICIOUS":
                level = "SUSPICIOUS"
            reasons.append(f"{ip} has AbuseIPDB score {score}%")

    for domain, rep in enrichment["domain_reputation"].items():
        if rep.get("query_status") == "is_host":
            level = "MALICIOUS"
            reasons.append(f"{domain} found in URLhaus ({rep.get('threat', 'unknown threat')})")

    if not reasons:
        reasons.append("No known threat indicators found")

    return {"level": level, "reasons": reasons}


# ── main ──────────────────────────────────────────────────────────────────────

def main(flow_path: str, output_path: str) -> None:
    print(f"Enriching {flow_path} -> {output_path}", flush=True)

    try:
        with open(flow_path) as f:
            events = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Failed to read flow file: {e}", flush=True)
        sys.exit(1)

    iocs = extract_iocs(events)

    enrichment = {
        "enriched_at":       datetime.now(timezone.utc).isoformat(),
        "iocs":              iocs,
        "ip_reputation":     {},
        "domain_reputation": {},
        "ja3_fingerprints":  {},
    }

    for ip in iocs["ips"]:
        print(f"  AbuseIPDB: {ip}", flush=True)
        enrichment["ip_reputation"][ip] = lookup_abuseipdb(ip)
        time.sleep(0.5)

    for domain in iocs["domains"]:
        print(f"  URLhaus: {domain}", flush=True)
        enrichment["domain_reputation"][domain] = lookup_urlhaus(domain)
        time.sleep(0.5)

    for ja3_hash in iocs["ja3s"]:
        print(f"  JA3: {ja3_hash}", flush=True)
        enrichment["ja3_fingerprints"][ja3_hash] = lookup_ja3(ja3_hash)
        time.sleep(0.5)

    enrichment["verdict"] = make_verdict(enrichment)

    with open(output_path, "w") as f:
        json.dump(enrichment, f, indent=2)

    print(f"  Done [verdict: {enrichment['verdict']['level']}]", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <flow_file> <enrichment_output_file>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
