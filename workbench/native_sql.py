"""Bounded, read-only SQLite execution over already authorized frozen databases.

Only the application resolves source handles to paths. This module is not a
model-facing file access API or an arbitrary-code sandbox.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time


VERSION = "native-sql-execution/2"
LEGACY_VERSION = "native-sql-execution/1"
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
MAX_ROWS = 500
MAX_COLUMNS = 128
MAX_VM_STEPS = 2_000_000
WALL_SECONDS = 8
_WORKERS = threading.BoundedSemaphore(1)
_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "glob", "group_concat", "hex", "ifnull",
    "iif", "instr", "length", "like", "lower", "ltrim", "max", "min", "nullif",
    "quote", "replace", "round", "rtrim", "sign", "substr", "substring", "sum",
    "total", "trim", "typeof", "unicode", "upper", "zeroblob",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
    "lag", "lead", "first_value", "last_value", "nth_value",
})
NUMERIC_SEMANTICS = (
    "SQLite storage classes, affinity, collation and integer arithmetic are "
    "preserved. REAL uses IEEE-754, not exact decimal arithmetic. SQLite division "
    "by zero returns NULL; casts can coerce text. NULL is not zero or evidence of "
    "non-disclosure. Reproducible execution does not certify financial meaning."
)


class NativeSqlError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message

    def as_dict(self):
        return {"error": self.code, "message": self.message}


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise NativeSqlError("invalid_native_encoding", "Native records require finite JSON values and valid Unicode") from exc


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 100 or "\0" in value:
        raise NativeSqlError("invalid_sql_input", "Invalid database alias")
    return '"' + value.replace('"', '""') + '"'


def _value(value, *, result=False, legacy=False):
    if value is None or type(value) is str:
        return value
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        if abs(value) > 2 ** 53 - 1 and not legacy:
            if result:
                return {"type": "integer", "decimal": str(value)}
            raise NativeSqlError("invalid_sql_input", "Large integer parameters must be decimal strings; cast explicitly in SQL")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if result and isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    raise NativeSqlError(
        "invalid_sql_result" if result else "invalid_sql_input",
        "SQL values must be finite numbers, strings or NULL; result BLOBs are encoded",
    )


def display_cell(value):
    if value is None:
        return "NULL"
    if isinstance(value, dict) and value.get("type") == "integer":
        return value["decimal"]
    return encoded(value).decode()


def storage_class(value):
    if isinstance(value, dict):
        return value["type"]
    return {type(None): "null", str: "text", int: "integer", float: "real"}[type(value)]


def display_query(query):
    return {
        "rows": [[display_cell(value) for value in row] for row in query["rows"]],
        "storage_classes": [[storage_class(value) for value in row] for row in query["rows"]],
        "parameters": encoded(query["parameters"]).decode(),
    }


def execute(sources, sql, parameters=None, *, row_limit=MAX_ROWS, cancelled=None, output_version=VERSION):
    """sources is an application-only list of {alias, path, source_id, sha256}."""
    if output_version not in (VERSION, LEGACY_VERSION):
        raise NativeSqlError("unsupported_sql_version", "The recorded SQL output contract is not supported")
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 16_000:
        raise NativeSqlError("invalid_sql_input", "SQL must contain 1..16000 characters")
    parameters = [] if parameters is None else parameters
    if not isinstance(parameters, list) or len(parameters) > 100:
        raise NativeSqlError("invalid_sql_input", "Use at most 100 positional parameters")
    for value in parameters:
        _value(value, legacy=output_version == LEGACY_VERSION)
    if type(row_limit) is not int or not 1 <= row_limit <= MAX_ROWS:
        raise NativeSqlError("invalid_sql_input", f"row_limit must be 1..{MAX_ROWS}")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise NativeSqlError("invalid_sql_input", "Select 1..8 registered snapshots")
    aliases = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"alias", "path", "source_id", "sha256"}:
            raise NativeSqlError("invalid_sql_input", "A resolved source descriptor is required")
        _identifier(source["alias"])
        alias = source["alias"].lower()
        if alias in aliases or alias in {"main", "temp"}:
            raise NativeSqlError("invalid_sql_input", "Database aliases must be distinct and nonreserved")
        aliases.add(alias)
        if (not isinstance(source["path"], str)
                or not Path(source["path"]).is_absolute()
                or not isinstance(source["source_id"], str)
                or not 1 <= len(source["source_id"]) <= 200
                or not isinstance(source["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None):
            raise NativeSqlError("invalid_sql_input", "Source identity, absolute path and hash are required")
    request = encoded({"sources": sources, "sql": sql, "parameters": parameters,
                       "row_limit": row_limit, "output_version": output_version})
    if len(request) > MAX_REQUEST_BYTES:
        raise NativeSqlError("sql_input_limit", "SQL request exceeds its byte limit")
    if cancelled is not None and cancelled():
        raise NativeSqlError("sql_cancelled", "Query cancelled before execution")
    if not _WORKERS.acquire(timeout=1):
        raise NativeSqlError("sql_executor_busy", "The bounded SQL worker is busy; retry later")
    try:
        with subprocess.Popen(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"LANG": "C.UTF-8"},
        ) as process:
            deadline = time.monotonic() + WALL_SECONDS
            first = True
            try:
                while True:
                    if cancelled is not None and cancelled():
                        raise NativeSqlError("sql_cancelled", "Query cancelled during execution")
                    if time.monotonic() >= deadline:
                        raise NativeSqlError("sql_execution_limit", "SQL worker exceeded its wall-clock limit")
                    try:
                        output, error = process.communicate(request if first else None, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        first = False
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            if process.returncode != 0:
                raise NativeSqlError("sql_worker_failed",
                                     f"SQL worker exited without a result (exit {process.returncode})")
            if error or len(output) > MAX_OUTPUT_BYTES:
                raise NativeSqlError("sql_worker_protocol", "SQL worker returned an invalid response")
            try:
                response = json.loads(output)
            except (ValueError, UnicodeError) as exc:
                raise NativeSqlError("sql_worker_protocol", "SQL worker returned invalid JSON") from exc
            if not isinstance(response, dict):
                raise NativeSqlError("sql_worker_protocol", "SQL worker returned no result object")
            if "error" in response:
                raise NativeSqlError(response["error"], response["message"])
            identity = response.pop("query_id", None)
            if response.get("version") != output_version or identity != "sql-" + digest(response):
                raise NativeSqlError("sql_worker_protocol", "SQL result identity is invalid")
            return {"query_id": identity, **response}
    finally:
        _WORKERS.release()


def _hash_file(handle):
    handle.seek(0)
    return hashlib.file_digest(handle, "sha256").hexdigest()


def _query(request):
    sources = request["sources"]
    with ExitStack() as stack:
        connection = stack.enter_context(closing(sqlite3.connect(":memory:")))
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 1_000)
        manifests, opened, allowed = [], [], {}
        for source in sources:
            descriptor = os.open(source["path"], os.O_RDONLY | os.O_NOFOLLOW)
            handle = stack.enter_context(os.fdopen(descriptor, "rb"))
            size = os.fstat(handle.fileno()).st_size
            if size > MAX_SOURCE_BYTES or handle.read(16) != b"SQLite format 3\0":
                raise NativeSqlError("invalid_sql_source", "Source is not a bounded SQLite snapshot")
            if _hash_file(handle) != source["sha256"]:
                raise NativeSqlError("sql_source_integrity", "Frozen database content has changed")
            alias = _identifier(source["alias"])
            uri = f"file:/proc/self/fd/{handle.fileno()}?mode=ro&immutable=1"
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
            tables = connection.execute(
                f"SELECT name FROM {alias}.sqlite_schema WHERE type IN ('table','view') "
                "AND name NOT GLOB 'sqlite_*'").fetchall()
            allowed[source["alias"]] = {row[0] for row in tables} | {"sqlite_master", "sqlite_schema"}
            manifests.append({key: source[key] for key in ("alias", "source_id", "sha256")})
            opened.append((handle, source["sha256"]))
        connection.execute("PRAGMA query_only=ON")
        reads = set()

        def authorize(action, first, second, database, context):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in _FUNCTIONS:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and first in allowed.get(database, set()):
                reads.add((database, first, second))
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        steps = 0

        def progress():
            nonlocal steps
            steps += 1_000
            return int(steps > MAX_VM_STEPS)

        connection.set_authorizer(authorize)
        connection.set_progress_handler(progress, 1_000)
        try:
            cursor = connection.execute(request["sql"], request["parameters"])
            if cursor.description is None:
                raise NativeSqlError("invalid_sql_query", "A read-only result set is required")
            columns = [{"index": i, "name": item[0]} for i, item in enumerate(cursor.description)]
            fetched = cursor.fetchmany(request["row_limit"] + 1)
            rows = [[_value(value, result=True, legacy=request["output_version"] == LEGACY_VERSION) for value in row]
                    for row in fetched[:request["row_limit"]]]
        except sqlite3.Error as exc:
            code = "sql_execution_limit" if steps > MAX_VM_STEPS else "sql_query_rejected"
            raise NativeSqlError(code, str(exc)) from exc
        for handle, expected in opened:
            if _hash_file(handle) != expected:
                raise NativeSqlError("sql_source_integrity", "Source changed during SQL execution")
        result = {
            "version": request["output_version"], "sqlite_version": sqlite3.sqlite_version,
            "sql": request["sql"], "parameters": request["parameters"],
            "sources": manifests, "columns": columns, "rows": rows,
            "row_limit": request["row_limit"],
            "truncated": len(fetched) > request["row_limit"],
            "reads": [{"alias": alias, "table": table, "column": column}
                      for alias, table, column in sorted(reads)],
            "numeric_semantics": NUMERIC_SEMANTICS,
            "ordering": "Row order is only promised when the SQL explicitly determines it.",
        }
        if request["output_version"] == VERSION:
            result["display"] = display_query(result)
        result["query_id"] = "sql-" + digest(result)
        if len(encoded(result)) > MAX_OUTPUT_BYTES:
            raise NativeSqlError("sql_output_limit", "Query output exceeds its byte limit; narrow the query")
        return result


def _worker():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    try:
        request = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
        result = _query(request)
    except NativeSqlError as exc:
        result = exc.as_dict()
    except OSError as exc:
        result = {"error": "sql_source_unavailable",
                  "message": f"SQL snapshot cannot be opened (OS error {exc.errno})"}
    except (sqlite3.Error, OverflowError, MemoryError) as exc:
        result = {"error": "sql_execution_failed", "message": str(exc) or type(exc).__name__}
    sys.stdout.buffer.write(encoded(result))


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This is an application-internal worker, not a file-query CLI.")
    _worker()
