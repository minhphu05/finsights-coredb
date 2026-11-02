from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    ForeignKey,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM as PostgreSQLEnum
from sqlalchemy.dialects.postgresql import JSON as PostgreSQLJSON
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP as PostgreSQLTimestamp
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.dialects.mysql import JSON as MySQLJSON
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import LONGTEXT, TINYINT


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _sqlalchemy_type(type_name: str, engine_name: str = "mysql", column: dict[str, Any] | None = None) -> Any:
    normalized = type_name.strip().lower()
    column = column or {}

    if engine_name == "postgresql":
        if normalized == "uuid":
            return PostgreSQLUUID(as_uuid=False)
        if normalized == "text[]":
            return ARRAY(Text())
        if normalized == "jsonb":
            return JSONB()
        if normalized == "json":
            return PostgreSQLJSON()
        if normalized == "enum":
            enum_name = column.get("enum_name")
            enum_values = column.get("enum_values")
            if not isinstance(enum_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", enum_name):
                raise ValueError(f"PostgreSQL enum needs a valid enum_name: {column!r}")
            if not isinstance(enum_values, list) or not enum_values or any(not isinstance(value, str) for value in enum_values):
                raise ValueError(f"PostgreSQL enum {enum_name!r} needs non-empty string enum_values")
            return PostgreSQLEnum(*enum_values, name=enum_name, create_type=True)

    match = re.fullmatch(r"([a-z]+)(?:\((\d+)(?:\s*,\s*(\d+))?\))?", normalized)
    if not match:
        raise ValueError(f"unsupported SQL type {type_name!r}")
    name, first, second = match.groups()
    length = int(first) if first else None
    scale = int(second) if second else None

    if name in {"varchar", "char", "binary", "varbinary"}:
        if not length:
            raise ValueError(f"{name} needs a length")
        if name == "char":
            return CHAR(length, **({"collation": "utf8mb4_unicode_ci"} if engine_name == "mysql" else {}))
        if name == "varchar":
            return String(length, **({"collation": "utf8mb4_unicode_ci"} if engine_name == "mysql" else {}))
        return String(length)
    if name == "tinyint":
        if engine_name == "postgresql":
            return SmallInteger()
        return TINYINT(display_width=length or 1)
    if name == "smallint":
        return SmallInteger()
    if name in {"int", "integer"}:
        return Integer()
    if name == "bigint":
        return BigInteger()
    if name in {"decimal", "numeric"}:
        if not length:
            raise ValueError(f"{name} needs precision")
        return Numeric(precision=length, scale=scale or 0)
    if name in {"bool", "boolean"}:
        return Boolean()
    if name == "date":
        return Date()
    if name in {"datetime", "timestamp"}:
        if engine_name == "postgresql":
            return PostgreSQLTimestamp(precision=length) if length is not None else PostgreSQLTimestamp()
        return MySQLDateTime(fsp=length) if length is not None else DateTime()
    if name == "text":
        return Text()
    if name == "longtext":
        return LONGTEXT()
    if name == "json":
        return PostgreSQLJSON() if engine_name == "postgresql" else MySQLJSON()
    raise ValueError(f"unsupported SQL type {type_name!r}")


def _server_default(value: Any, engine_name: str = "mysql") -> Any:
    if isinstance(value, bool):
        return text("TRUE" if value else "FALSE")
    if isinstance(value, (int, float)):
        return text(str(value))
    if isinstance(value, list) and engine_name == "postgresql" and all(isinstance(item, str) for item in value):
        escaped = ["'" + item.replace("'", "''") + "'" for item in value]
        return text("ARRAY[" + ", ".join(escaped) + "]")
    if not isinstance(value, str):
        raise ValueError(f"unsupported server default {value!r}")
    normalized = value.upper()
    if normalized in {"CURRENT_TIMESTAMP", "CURRENT_DATE", "TRUE", "FALSE", "NULL"} or re.fullmatch(r"CURRENT_TIMESTAMP\(\d+\)", normalized):
        return text(normalized)
    return text("'" + value.replace("'", "''") + "'")


def _index_name(table: str, columns: list[str], unique: bool) -> str:
    prefix = "uq" if unique else "ix"
    base = f"{prefix}_{table}_{'_'.join(columns)}"
    if len(base) <= 60:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[:51]}_{digest}"


def load_metadata(
    schema_paths: list[Path],
    managed_comment: str = "managed-by=finsights-coredb",
    engine_name: str = "mysql",
) -> MetaData:
    merged: dict[str, dict[str, Any]] = {}
    for path in schema_paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict) or schema.get("version") != 1:
            raise ValueError(f"{path.name} must have version: 1")
        enum_definitions = schema.get("enums", {})
        if not isinstance(enum_definitions, dict):
            raise ValueError(f"{path.name} enums must be an object")
        for table in schema.get("tables", []):
            name = table.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid table name in {path.name}")
            current = merged.setdefault(name, {"name": name, "columns": [], "indexes": []})
            known_columns = {column["name"] for column in current["columns"]}
            for column in table.get("columns", []):
                if column.get("type", "").lower() == "enum" and "enum_values" not in column:
                    enum_values = enum_definitions.get(column.get("enum_name"))
                    if enum_values is None:
                        raise ValueError(f"enum definition missing for {table['name']}.{column['name']} in {path.name}")
                    column = {**column, "enum_values": enum_values}
                if column["name"] in known_columns:
                    raise ValueError(f"duplicate column {name}.{column['name']} in schema files")
                current["columns"].append(column)
                known_columns.add(column["name"])
            current["indexes"].extend(table.get("indexes", []))

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    for table_name, spec in merged.items():
        columns = []
        for item in spec["columns"]:
            kwargs: dict[str, Any] = {
                "nullable": item.get("nullable", True),
                "primary_key": item.get("primary_key", False),
            }
            if item.get("autoincrement"):
                kwargs["autoincrement"] = True
            if item.get("unique"):
                kwargs["unique"] = True
            if "default" in item:
                kwargs["server_default"] = _server_default(item["default"], engine_name)
            reference = item.get("references")
            args: list[Any] = [_sqlalchemy_type(item["type"], engine_name, item)]
            if reference:
                args.append(ForeignKey(
                    f"{reference['table']}.{reference['column']}",
                    ondelete=reference.get("on_delete"),
                ))
            columns.append(Column(item["name"], *args, **kwargs))
        table = Table(table_name, metadata, *columns, comment=managed_comment)
        for index in spec["indexes"]:
            index_columns = index.get("columns", [])
            if not index_columns or any(col not in table.c for col in index_columns):
                raise ValueError(f"invalid index columns on {table_name}: {index_columns}")
            unique = bool(index.get("unique", False))
            Index(_index_name(table_name, index_columns, unique), *(table.c[col] for col in index_columns), unique=unique)

    metadata.sorted_tables
    return metadata
