"""
filter_model — pure data layer for the filter/sort system.

No Qt, no federation, no SQLite imports. Just dataclasses, field registries,
and SQL fragment builders. All queries that need WHERE/ORDER BY clauses call
build_filter_conditions / build_sort_clause and splice the result in.

Table alias convention: callers pass alias='a' (list_filtered_assets) or
alias='all_assets' (list_tags_for_filtered_assets, which names the table
explicitly). The {alias} placeholder in sql_expr is substituted at build time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FilterField:
    id: str
    display_name: str
    # text | integer | boolean | tag | dataset | sql
    dtype: str
    # SQL expression for the value; use {alias} for the assets table alias.
    # Empty for tag/dataset/sql — those are generated inline.
    sql_expr: str


@dataclass
class SortField:
    id: str
    display_name: str
    # Bare column name, or any SQL expression using {alias} for the assets
    # table alias (substituted at build time, as in FilterField.sql_expr).
    sql_expr: str


@dataclass
class FilterRule:
    field_id: str
    op: str
    value: str            # always stored as a string


@dataclass
class SortRule:
    field_id: str
    desc: bool


# Operator menus per dtype — list of (op_id, display_label).
DTYPE_OPS: dict[str, list[tuple[str, str]]] = {
    "text":    [("=", "equals"), ("!=", "not equals"),
                ("contains", "contains"), ("starts_with", "starts with")],
    "integer": [("=", "="), ("!=", "≠"), (">", ">"), ("<", "<"),
                (">=", "≥"), ("<=", "≤")],
    # ISO-8601 strings compare lexicographically, so the integer operator set
    # works verbatim; values bind as text.
    "timestamp": [("=", "on"), ("!=", "not on"),
                  (">", "after"), ("<", "before"),
                  (">=", "on or after"), ("<=", "on or before"),
                  ("starts_with", "starts with"), ("contains", "contains")],
    "boolean": [("is_true", "is true"), ("is_false", "is false")],
    "tag":     [("has", "has tag"), ("not_has", "does not have tag")],
    "dataset": [("is_in", "is in dataset"), ("not_in", "not in dataset")],
    "sql":     [("sql", "matches SQL")],
}

FILTER_FIELDS: dict[str, FilterField] = {
    f.id: f for f in [
        FilterField("tag",           "Tag",                   "tag",     ""),
        FilterField("dataset",       "Dataset",               "dataset", ""),
        FilterField("format",        "Format",                "text",    "{alias}.format"),
        FilterField("width",         "Width (px)",            "integer", "{alias}.width"),
        FilterField("height",        "Height (px)",           "integer", "{alias}.height"),
        FilterField("bytes",         "File size (bytes)",     "integer", "{alias}.bytes"),
        FilterField("exists_flag",   "File exists",           "boolean", "{alias}.exists_flag"),
        FilterField("has_mask",      "Has mask",              "boolean", "{alias}.has_mask"),
        FilterField("tags_validated","Tags validated",        "boolean", "{alias}.tags_validated"),
        # EAV-backed booleans (image_attributes). Presence of value '1' = true.
        FilterField("is_favorite",   "Favorite",              "boolean",
            "(SELECT 1 FROM all_image_attributes"
            " WHERE asset_id = {alias}.asset_id AND key = 'is_favorite' AND value = '1')"),
        # EAV-backed timestamp: one shared value per scan batch (see
        # imgdb.ATTR_SCAN_AT). ISO-8601 sorts and compares lexicographically.
        FilterField("scan_at",       "Scanned at",            "timestamp",
            "(SELECT value FROM all_image_attributes"
            " WHERE asset_id = {alias}.asset_id AND key = 'scan_at')"),
        FilterField("tag_count",     "Tag count",             "integer",
            "(SELECT COUNT(*) FROM all_asset_tags WHERE asset_id = {alias}.asset_id)"),
        FilterField("caption_count", "Caption count",         "integer",
            "(SELECT COUNT(*) FROM all_captions WHERE asset_id = {alias}.asset_id)"),
        FilterField("validated_captions", "Validated captions", "integer",
            "(SELECT COUNT(*) FROM all_captions"
            " WHERE asset_id = {alias}.asset_id AND is_validated = 1)"),
        FilterField("duplicate_count", "Duplicate count",    "integer",
            "(SELECT COUNT(*) FROM all_assets"
            " WHERE file_hash = {alias}.file_hash"
            " AND asset_id != {alias}.asset_id)"),
        FilterField("perceptual_duplicate_count", "Perceptual duplicates", "integer",
            "(SELECT COUNT(*) FROM all_assets"
            " WHERE perceptual_hash = {alias}.perceptual_hash"
            " AND asset_id != {alias}.asset_id"
            " AND {alias}.perceptual_hash IS NOT NULL"
            " AND {alias}.perceptual_hash != '')"),
        FilterField("sql",           "Custom SQL",            "sql",     ""),
    ]
}

SORTABLE_FIELDS: dict[str, SortField] = {
    f.id: f for f in [
        SortField("rel_path",     "Path",         "rel_path"),
        SortField("_root",        "Root",         "_root"),
        SortField("file_hash",        "File hash",      "file_hash"),
        SortField("perceptual_hash",  "Perceptual hash", "perceptual_hash"),
        SortField("format",       "Format",       "format"),
        SortField("width",        "Width",        "width"),
        SortField("height",       "Height",       "height"),
        SortField("bytes",        "File size",    "bytes"),
        SortField("created_at",   "Created",      "created_at"),
        SortField("updated_at",   "Updated",      "updated_at"),
        SortField("last_seen",    "Last seen",    "last_seen"),
        SortField("scan_at",      "Scanned at",
            "(SELECT value FROM all_image_attributes"
            " WHERE asset_id = {alias}.asset_id AND key = 'scan_at')"),
    ]
}


def build_filter_conditions(
    rules: list[FilterRule],
    labels: Optional[list[str]],
    alias: str = "a",
) -> tuple[list[str], list]:
    """
    Translate FilterRules + checked root labels into (conditions, params).

    The caller splices conditions into a WHERE clause with AND.
    labels=None means "all roots" (no shard filter). labels=[] short-circuits
    to a guaranteed-empty result via a sentinel condition.
    """
    conditions: list[str] = []
    params: list = []

    if labels is not None:
        if not labels:
            return ["1=0"], []
        ph = ",".join("?" * len(labels))
        conditions.append(f"{alias}._root IN ({ph})")
        params.extend(labels)

    for rule in rules:
        field = FILTER_FIELDS.get(rule.field_id)
        if field is None:
            continue

        if field.dtype == "boolean":
            expr = field.sql_expr.replace("{alias}", alias)
            if rule.op == "is_true":
                conditions.append(f"{expr} = 1")
            else:
                conditions.append(f"COALESCE({expr}, 0) != 1")

        elif field.dtype in ("text", "integer", "timestamp"):
            expr = field.sql_expr.replace("{alias}", alias)
            if rule.op == "contains":
                conditions.append(f"{expr} LIKE ?")
                params.append(f"%{rule.value}%")
            elif rule.op == "starts_with":
                conditions.append(f"{expr} LIKE ?")
                params.append(f"{rule.value}%")
            else:
                conditions.append(f"{expr} {rule.op} ?")
                if field.dtype == "integer":
                    try:
                        params.append(int(rule.value))
                    except (ValueError, TypeError):
                        params.append(rule.value)
                else:
                    params.append(rule.value)

        elif field.dtype == "tag":
            subq = (
                "SELECT tat.asset_id FROM all_asset_tags tat"
                " JOIN all_tags tfl ON tat.tag_id = tfl.tag_id"
                " AND tat._root = tfl._root WHERE tfl.name = ?"
            )
            if rule.op == "not_has":
                conditions.append(f"{alias}.asset_id NOT IN ({subq})")
            else:
                conditions.append(f"{alias}.asset_id IN ({subq})")
            params.append(rule.value)

        elif field.dtype == "dataset":
            subq = "SELECT asset_id FROM all_dataset_assets WHERE dataset_name = ?"
            if rule.op == "not_in":
                conditions.append(f"{alias}.asset_id NOT IN ({subq})")
            else:
                conditions.append(f"{alias}.asset_id IN ({subq})")
            params.append(rule.value)

        elif field.dtype == "sql":
            v = rule.value.strip()
            if v:
                conditions.append(f"({v})")

    return conditions, params


def build_sort_clause(rules: list[SortRule], alias: str = "a") -> str:
    """
    Convert SortRules to the fragment that follows ORDER BY (no keyword).
    Always appends asset_id ASC as a deterministic tiebreaker.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        field = SORTABLE_FIELDS.get(rule.field_id)
        if field is None or field.id in seen:
            continue
        seen.add(field.id)
        expr = field.sql_expr.replace("{alias}", alias)
        parts.append(f"{expr} {'DESC' if rule.desc else 'ASC'}")
    parts.append("asset_id ASC")
    return ", ".join(parts)
