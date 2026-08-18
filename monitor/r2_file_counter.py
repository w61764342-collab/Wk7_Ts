"""
r2_file_counter.py
==================
Count objects and byte sizes stored in Cloudflare R2 for the monitor hub dashboard.

Per scraper: all objects under the scraper's R2 data prefix (all dates, all types).
Per site: all objects under the site's r2_prefix (includes monitor/ metadata).

Daily size sums objects whose key contains a date partition (year=/month=/day=)
or whose LastModified date (UTC) matches the report date — KCSB data uses the
latter because files are not stored under date folders.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterator

log = logging.getLogger("monitor")

FILE_TYPE_CATEGORIES = ("images", "json", "excel", "csv", "parquet", "other")

_EXTENSION_TO_CATEGORY: dict[str, str] = {
    ".jpg": "images",
    ".jpeg": "images",
    ".png": "images",
    ".webp": "images",
    ".gif": "images",
    ".bmp": "images",
    ".tiff": "images",
    ".svg": "images",
    ".json": "json",
    ".xlsx": "excel",
    ".xls": "excel",
    ".xlsm": "excel",
    ".csv": "csv",
    ".parquet": "parquet",
}


def _normalise_list_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    return f"{normalized}/" if normalized else ""


def _parse_partition_date(partition_dt: datetime | date | str) -> date:
    if isinstance(partition_dt, datetime):
        return partition_dt.date()
    if isinstance(partition_dt, date):
        return partition_dt
    return datetime.fromisoformat(str(partition_dt)).date()


def _date_partition_variants(partition_dt: datetime | date | str) -> tuple[str, str]:
    """Padded and unpadded Hive-style date path segments for the same calendar day."""
    day = _parse_partition_date(partition_dt)
    padded = f"year={day.year}/month={day.month:02d}/day={day.day:02d}"
    unpadded = f"year={day.year}/month={day.month}/day={day.day}"
    return padded, unpadded


def _empty_inventory() -> dict[str, Any]:
    return {
        "objects": 0,
        "size_bytes": 0,
        "by_type_objects": {category: 0 for category in FILE_TYPE_CATEGORIES},
        "by_type_bytes": {category: 0 for category in FILE_TYPE_CATEGORIES},
    }


def _file_category(key: str) -> str:
    extension = PurePosixPath(key).suffix.lower()
    return _EXTENSION_TO_CATEGORY.get(extension, "other")


def _accumulate_object(inventory: dict[str, Any], obj: dict) -> None:
    size = int(obj.get("Size") or 0)
    category = _file_category(obj.get("Key", ""))
    inventory["objects"] += 1
    inventory["size_bytes"] += size
    inventory["by_type_objects"][category] += 1
    inventory["by_type_bytes"][category] += size


def _resolve_scraper_base(r2_base: str, category_slug: str | None = None) -> str:
    base = r2_base.strip("/")
    if not base:
        return ""
    if category_slug:
        slug = category_slug.strip("/")
        if slug:
            base = f"{base}/{slug}"
    return base


def _iter_r2_objects(client: Any, bucket: str, prefix: str) -> Iterator[dict]:
    """Yield S3 object metadata dicts under *prefix* (skips folder markers)."""
    list_prefix = _normalise_list_prefix(prefix)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key or key.endswith("/"):
                continue
            yield obj


def _object_matches_daily_partition(obj: dict, partition_dt: datetime | date | str) -> bool:
    key = obj.get("Key", "")
    for partition in _date_partition_variants(partition_dt):
        if f"/{partition}/" in key or key.startswith(partition):
            return True

    modified = obj.get("LastModified")
    if modified is None:
        return False
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)
    return modified.astimezone(timezone.utc).date() == _parse_partition_date(partition_dt)


def count_r2_inventory_by_type(client: Any, bucket: str, prefix: str) -> dict[str, Any]:
    """Count objects and byte sizes under *prefix*, grouped by file extension category."""
    inventory = _empty_inventory()
    try:
        for obj in _iter_r2_objects(client, bucket, prefix):
            _accumulate_object(inventory, obj)
    except Exception as exc:
        log.warning(
            "R2 inventory by type failed for prefix %r: %s",
            _normalise_list_prefix(prefix),
            exc,
        )
        return _empty_inventory()
    return inventory


def count_daily_r2_inventory_by_type(
    client: Any,
    bucket: str,
    r2_base: str,
    partition_dt: datetime | date | str,
    category_slug: str | None = None,
) -> dict[str, Any]:
    """Inventory for one report date under a scraper/site prefix (single pass, deduped by key)."""
    base = _resolve_scraper_base(r2_base, category_slug)
    if not base:
        return _empty_inventory()

    inventory = _empty_inventory()
    seen_keys: set[str] = set()
    try:
        for obj in _iter_r2_objects(client, bucket, base):
            key = obj.get("Key", "")
            if not key or key in seen_keys:
                continue
            if not _object_matches_daily_partition(obj, partition_dt):
                continue
            seen_keys.add(key)
            _accumulate_object(inventory, obj)
    except Exception as exc:
        log.warning("R2 daily inventory by type failed for prefix %r: %s", base, exc)
        return _empty_inventory()

    log.debug(
        "  R2 daily inventory by type %s (%s): %d object(s), %d byte(s)",
        base,
        partition_dt,
        inventory["objects"],
        inventory["size_bytes"],
    )
    return inventory


def count_scraper_r2_inventory_by_type(
    client: Any,
    bucket: str,
    r2_base: str,
    category_slug: str | None = None,
) -> dict[str, Any]:
    """Total inventory under one scraper/category prefix, grouped by file type."""
    base = _resolve_scraper_base(r2_base, category_slug)
    if not base:
        return _empty_inventory()
    inventory = count_r2_inventory_by_type(client, bucket, base)
    log.debug(
        "  R2 inventory by type %s: %d object(s), %d byte(s)",
        base,
        inventory["objects"],
        inventory["size_bytes"],
    )
    return inventory


def count_site_r2_inventory_by_type(client: Any, bucket: str, r2_prefix: str) -> dict[str, Any]:
    """Total inventory under the site's data prefix, grouped by file type."""
    prefix = r2_prefix.strip("/")
    if not prefix:
        return _empty_inventory()
    inventory = count_r2_inventory_by_type(client, bucket, prefix)
    log.info(
        "Site R2 inventory by type (%s): %d object(s), %d byte(s)",
        prefix,
        inventory["objects"],
        inventory["size_bytes"],
    )
    return inventory


def count_r2_objects(client: Any, bucket: str, prefix: str) -> int:
    """Count all objects under *prefix* using paginated list_objects_v2."""
    try:
        return sum(1 for _ in _iter_r2_objects(client, bucket, prefix))
    except Exception as exc:
        log.warning("R2 object count failed for prefix %r: %s", _normalise_list_prefix(prefix), exc)
        return 0


def sum_r2_size_bytes(client: Any, bucket: str, prefix: str) -> int:
    """Sum byte sizes of all objects under *prefix*."""
    try:
        return sum(int(obj.get("Size") or 0) for obj in _iter_r2_objects(client, bucket, prefix))
    except Exception as exc:
        log.warning("R2 size sum failed for prefix %r: %s", _normalise_list_prefix(prefix), exc)
        return 0


def count_scraper_r2_files(
    client: Any,
    bucket: str,
    r2_base: str,
    category_slug: str | None = None,
) -> int:
    """Total objects under one scraper/category prefix."""
    base = _resolve_scraper_base(r2_base, category_slug)
    if not base:
        return 0
    total = count_r2_objects(client, bucket, base)
    log.debug("  R2 inventory %s: %d object(s)", base, total)
    return total


def count_scraper_r2_size_bytes(
    client: Any,
    bucket: str,
    r2_base: str,
    category_slug: str | None = None,
) -> int:
    """Total byte size under one scraper/category prefix."""
    base = _resolve_scraper_base(r2_base, category_slug)
    if not base:
        return 0
    total = sum_r2_size_bytes(client, bucket, base)
    log.debug("  R2 size %s: %d byte(s)", base, total)
    return total


def count_scraper_r2_daily_size_bytes(
    client: Any,
    bucket: str,
    r2_base: str,
    partition_dt: datetime | date | str,
    category_slug: str | None = None,
) -> int:
    """Byte size of objects for one scraper on the given report date."""
    base = _resolve_scraper_base(r2_base, category_slug)
    if not base:
        return 0
    try:
        total = sum(
            int(obj.get("Size") or 0)
            for obj in _iter_r2_objects(client, bucket, base)
            if _object_matches_daily_partition(obj, partition_dt)
        )
    except Exception as exc:
        log.warning("R2 daily size failed for prefix %r: %s", base, exc)
        return 0
    log.debug("  R2 daily size %s (%s): %d byte(s)", base, partition_dt, total)
    return total


def count_site_r2_files(client: Any, bucket: str, r2_prefix: str) -> int:
    """Total objects under the site's data prefix (all scrapers + monitor artifacts)."""
    prefix = r2_prefix.strip("/")
    if not prefix:
        return 0
    total = count_r2_objects(client, bucket, prefix)
    log.info("Site R2 inventory (%s): %d object(s)", prefix, total)
    return total


def count_site_r2_size_bytes(client: Any, bucket: str, r2_prefix: str) -> int:
    """Total byte size under the site's data prefix."""
    prefix = r2_prefix.strip("/")
    if not prefix:
        return 0
    total = sum_r2_size_bytes(client, bucket, prefix)
    log.info("Site R2 size (%s): %d byte(s)", prefix, total)
    return total


def count_site_r2_daily_size_bytes(
    client: Any,
    bucket: str,
    r2_prefix: str,
    partition_dt: datetime | date | str,
) -> int:
    """Byte size of site objects for the given report date."""
    prefix = r2_prefix.strip("/")
    if not prefix:
        return 0
    try:
        total = sum(
            int(obj.get("Size") or 0)
            for obj in _iter_r2_objects(client, bucket, prefix)
            if _object_matches_daily_partition(obj, partition_dt)
        )
    except Exception as exc:
        log.warning("Site R2 daily size failed for prefix %r: %s", prefix, exc)
        return 0
    log.info("Site R2 daily size (%s, %s): %d byte(s)", prefix, partition_dt, total)
    return total
