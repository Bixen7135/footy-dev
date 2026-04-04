from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

PRODUCT_MARKER_WHERE = """
lower(coalesce(name, '')) = 'api boot'
or lower(coalesce(name, '')) like 'demo%'
or lower(coalesce(name, '')) like 'facet%'
or lower(coalesce(name, '')) like 'multicat%'
or lower(coalesce(name, '')) like 'test%'
or lower(coalesce(slug, '')) like 'demo-%'
or lower(coalesce(slug, '')) like 'facet-%'
or lower(coalesce(slug, '')) like 'multi-%'
or lower(coalesce(slug, '')) like 'test-%'
"""

CATEGORY_MARKER_WHERE = """
lower(coalesce(name, '')) like 'demo%'
or lower(coalesce(name, '')) like 'facet%'
or lower(coalesce(name, '')) like 'multicat%'
or lower(coalesce(name, '')) like 'test%'
or lower(coalesce(slug, '')) = 'demo-footwear'
or lower(coalesce(slug, '')) = 'test-cat'
or lower(coalesce(slug, '')) like 'demo-%'
or lower(coalesce(slug, '')) like 'facet-%'
or lower(coalesce(slug, '')) like 'multi-%'
or lower(coalesce(slug, '')) like 'test-%'
"""


def _count(cursor: sqlite3.Cursor, query: str) -> int:
    cursor.execute(query)
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def _resolve_db_path(raw_db_path: str) -> Path:
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    return db_path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert that working DB does not contain test/demo products or categories."
    )
    parser.add_argument(
        "--db-path",
        default="./footy.db",
        help="Path to sqlite DB. Defaults to ./footy.db (relative to current working directory).",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        metrics = {
            "db_path": str(db_path),
            "products": _count(cur, "select count(*) from products"),
            "categories": _count(cur, "select count(*) from categories"),
            "source_catalog_items": _count(cur, "select count(*) from source_catalog_items"),
            "product_images": _count(cur, "select count(*) from product_images"),
            "product_variants": _count(cur, "select count(*) from product_variants"),
            "flagged_products": _count(cur, f"select count(*) from products where {PRODUCT_MARKER_WHERE}"),
            "flagged_categories": _count(cur, f"select count(*) from categories where {CATEGORY_MARKER_WHERE}"),
        }

        cur.execute(
            f"""
            select id, slug, name
            from products
            where {PRODUCT_MARKER_WHERE}
            order by id desc
            limit 10
            """
        )
        metrics["flagged_product_examples"] = [
            {"id": int(row[0]), "slug": row[1], "name": row[2]} for row in cur.fetchall()
        ]

        cur.execute(
            f"""
            select id, slug, name
            from categories
            where {CATEGORY_MARKER_WHERE}
            order by id desc
            limit 10
            """
        )
        metrics["flagged_category_examples"] = [
            {"id": int(row[0]), "slug": row[1], "name": row[2]} for row in cur.fetchall()
        ]
    finally:
        conn.close()

    print(json.dumps(metrics))

    if metrics["flagged_products"] > 0 or metrics["flagged_categories"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
