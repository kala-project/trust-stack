"""Model-facing handles for native data; paths and ingestion stay application-only."""
from .native_sql import NativeSqlError, NUMERIC_SEMANTICS, display_cell, encoded, storage_class
from .research_tools import ToolError


def definitions(service, *, owner, run_id, scope, cancelled):
    def catalog(offset=0, limit=10):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise NativeSqlError("invalid_native_input", "Use a nonnegative offset and a limit from 1 to 20")
        sources = service.list_sources(owner=owner, scope=scope)
        selected = sources[offset:offset + limit]
        return {
            "sources": [{key: item[key] for key in (
                "snapshot_id", "source_key", "title", "revision", "media_type", "origin", "registered_at")}
                for item in selected],
            "total": len(sources), "next_offset": offset + len(selected) if offset + len(selected) < len(sources) else None,
            "scope": "Registered public sources and this conversation's private sources; not limited to primary research subjects.",
            "absence": "An empty catalog means no native snapshot is available in this research version, not non-disclosure.",
        }

    def describe(snapshot_id):
        if snapshot_id not in scope["snapshot_ids"]:
            raise NativeSqlError("native_source_out_of_scope", "Snapshot is outside this research version")
        return service.source(snapshot_id, owner=owner)

    def query(sources, sql, parameters=None, row_limit=50):
        result = service.query(owner=owner, run_id=run_id, scope=scope, sources=sources,
                               sql=sql, parameters=parameters, row_limit=row_limit, cancelled=cancelled)
        if len(encoded(result)) > 60_000:
            raise NativeSqlError("native_tool_output_limit", "Query was recorded but is too large for publication context; narrow its rows or columns")
        return result

    def wrap(handler):
        def call(**kwargs):
            try:
                return handler(**kwargs)
            except NativeSqlError as exc:
                raise ToolError(exc.code, exc.message) from exc
        return call

    return [
        {
            "name": "list_native_sources",
            "description": "Discover registered native database snapshots without acquisition or filesystem access. Source permissions are independent of research targets.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            }},
            "handler": wrap(catalog),
        },
        {
            "name": "describe_native_source",
            "description": "Read the original database schema, snapshot identity and declared origin. Schema entries retain provider names and SQL types, not financial-fact conversions.",
            "parameters": {"type": "object", "additionalProperties": False, "required": ["snapshot_id"],
                           "properties": {"snapshot_id": {"type": "string"}}},
            "handler": wrap(describe),
        },
        {
            "name": "execute_native_sql",
            "description": (
                "Run one read-only SQLite query against explicitly selected frozen database handles; "
                "qualify tables using your aliases. Joins, grouping, CTEs and window functions are supported. "
                "No writes, ATTACH, PRAGMA, extension loading or nondeterministic functions. "
                "The bounded result is persisted privately for this conversation and research version. "
                "Publish individual values with query_cell parts using query_id, zero-based row and column. "
                "Large INTEGER outputs carry exact decimal strings; display retains each native storage class. "
                "Pass large integer parameters as strings and cast explicitly, never as unsafe JSON numbers. "
                "Do not infer financial units from SQL types. " + NUMERIC_SEMANTICS
            ),
            "parameters": {"type": "object", "additionalProperties": False, "required": ["sources", "sql"],
                "properties": {
                    "sources": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                        "type": "object", "additionalProperties": False, "required": ["snapshot_id", "alias"],
                        "properties": {"snapshot_id": {"type": "string"}, "alias": {"type": "string", "minLength": 1, "maxLength": 100}},
                    }},
                    "sql": {"type": "string", "minLength": 1, "maxLength": 16000},
                    "parameters": {"type": "array", "maxItems": 100,
                                   "items": {"type": ["string", "number", "null"]}},
                    "row_limit": {"type": "integer", "minimum": 1, "maximum": 500},
                }},
            "handler": wrap(query),
        },
    ]


def cell_binding(part, record):
    row, column = part.get("row"), part.get("column")
    if (type(row) is not int or type(column) is not int or row < 0 or column < 0
            or row >= len(record["rows"]) or column >= len(record["columns"])):
        raise ToolError("invalid_report_value", "Native cell position is outside the recorded query result")
    value = record["rows"][row][column]
    # JSON literals expose native types; Unicode escapes keep source text from
    # becoming report markup or a manufactured citation.
    text = display_cell(value)
    for char in "[]*_`|<>#":
        text = text.replace(char, f"\\u{ord(char):04x}")
    return {
        "kind": "query_cell", "id": record["query_id"], "row": row, "column": column,
        "value": value, "text": text, "unit": None,
        "reference": {"kind": "query", "id": record["query_id"]},
        "dimensions": {"native_column": record["columns"][column]["name"],
                       "native_type": storage_class(value),
                       "row": row, "column": column, "truncated": record["truncated"]},
        "rendering": "JSON literal with Markdown metacharacters escaped; SQL NULL is displayed as NULL.",
    }
