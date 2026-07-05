"""
Helpers to count and aggregate request metrics from per-scraper JSON summaries
placed under `{r2_prefix}/{scraper}/year=YYYY/month=MM/day=DD/json-files/summary_YYYYMMDD.json`.
This is a compact implementation suitable for non-hub repos — it reads JSON summary
files from R2 and aggregates basic HTTP request counters.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any


def _parse_date(dt: str) -> datetime:
    # accept YYYY-MM-DD or ISO
    return datetime.fromisoformat(dt)


def _safe_get(d: dict, keys: list[str]) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def count_scraper_request_metrics(r2_client, bucket: str, r2_base: str, partition_dt: str) -> dict:
    """
    Scan the given scraper prefix (`r2_base`) for JSON summary files under
    `year=YYYY/month=MM/day=DD/json-files/` for the provided partition date
    (string YYYY-MM-DD). Returns a dict with aggregated metrics.
    """
    # Build expected prefix for json-files
    dt = _parse_date(partition_dt)
    json_prefix = PurePosixPath(r2_base) / f"year={dt.year}" / f"month={dt.month:02d}" / f"day={dt.day:02d}" / "json-files/"
    prefix = str(json_prefix).lstrip("/")

    paginator = r2_client.get_paginator("list_objects_v2")
    requests_total = 0
    requests_failed = 0
    duration_total = 0.0
    cache_hits = 0
    found = False
    failed_items_summary = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".json"):
                continue
            body = r2_client.get_object(Bucket=bucket, Key=key)["Body"].read()
            try:
                data = json.loads(body)
            except Exception:
                continue
            found = True
            rm = data.get("request_metrics") or data.get("stats") or {}
            rt = _safe_get(rm, ["requests_total", "total_http_requests", "scrape_do_requests", "request_count"]) or 0
            rf = _safe_get(rm, ["requests_failed", "failed_requests", "http_errors", "errors_count"]) or 0
            dur = _safe_get(rm, ["duration_sec", "elapsed_seconds", "scrape_duration_sec"]) or 0
            ch = rm.get("cache_hits") or 0
            requests_total += int(rt)
            requests_failed += int(rf)
            try:
                duration_total += float(dur)
            except Exception:
                pass
            try:
                cache_hits += int(ch)
            except Exception:
                pass
            if rm.get("failed_items"):
                for it in rm.get("failed_items"):
                    name = it.get("name") or it.get("slug") or it.get("category") or "unknown"
                    errors = it.get("errors") or 0
                    detail = it.get("detail") or ""
                    failed_items_summary.append(f"{name}: {errors} error(s) ({detail})")

    metrics = {}
    if not found:
        metrics["metrics_source"] = "none"
        return metrics

    metrics["requests_total"] = requests_total
    metrics["requests_failed"] = requests_failed
    metrics["duration_sec"] = duration_total
    metrics["cache_hits"] = cache_hits
    metrics["failed_items_summary"] = "; ".join(failed_items_summary) if failed_items_summary else None
    metrics["metrics_source"] = "json_summary"
    metrics["requests_per_min"] = (
        round(requests_total / (duration_total / 60), 2) if duration_total and requests_total else None
    )
    metrics["error_rate_pct"] = (
        round(requests_failed / requests_total * 100, 2) if requests_total else None
    )
    return metrics


def aggregate_site_request_metrics(all_results: list[dict]) -> dict:
    """Aggregate per-scraper results into a site-level metrics summary."""
    total_requests = 0
    total_failed = 0
    total_duration = 0.0
    count_with_duration = 0
    scrapers_failed = 0

    for r in all_results:
        rt = r.get("requests_total") or 0
        rf = r.get("requests_failed") or 0
        dur = r.get("duration_sec") or 0
        total_requests += rt
        total_failed += rf
        try:
            total_duration += float(dur)
            if dur:
                count_with_duration += 1
        except Exception:
            pass
        if r.get("requests_failed") and (r.get("requests_failed") > 0 or r.get("error_rate_pct") and r.get("error_rate_pct") > 0):
            scrapers_failed += 1

    site_metrics = {
        "requests_total": total_requests,
        "requests_failed": total_failed,
    }
    site_metrics["requests_per_min"] = (
        round(total_requests / (total_duration / 60), 2) if total_duration and total_requests else None
    )
    site_metrics["error_rate_pct"] = (
        round(total_failed / total_requests * 100, 2) if total_requests else None
    )
    site_metrics["scrapers_failed"] = scrapers_failed
    return site_metrics


def build_run_error_summary(all_results: list[dict], alerts: dict | None = None) -> dict:
    """Return a compact error_summary compatible with the monitor contract."""
    scrapers_total = len(all_results)
    scrapers_failed = sum(1 for r in all_results if not r.get("all_passed", True))
    scrapers_passed = scrapers_total - scrapers_failed
    validation_fail_rate_pct = None
    if scrapers_total:
        validation_fail_rate_pct = round((scrapers_failed / scrapers_total) * 100, 2)

    http = {
        "requests_total": sum(r.get("requests_total") or 0 for r in all_results),
        "requests_failed": sum(r.get("requests_failed") or 0 for r in all_results),
    }
    http["error_rate_pct"] = (
        round(http["requests_failed"] / http["requests_total"] * 100, 2)
        if http["requests_total"]
        else None
    )
    http["requests_per_min"] = None

    failed_scrapers = []
    for r in all_results:
        if not r.get("all_passed", True):
            failed_scrapers.append({
                "scraper": r.get("scraper") or r.get("name") or "unknown",
                "reason": r.get("reason") or "validation_failed",
                "requests_failed": r.get("requests_failed"),
            })

    summary = {
        "scrapers_total": scrapers_total,
        "scrapers_failed": scrapers_failed,
        "scrapers_passed": scrapers_passed,
        "validation_fail_rate_pct": validation_fail_rate_pct,
        "failed_scrapers": failed_scrapers,
        "http": http,
    }
    return summary
