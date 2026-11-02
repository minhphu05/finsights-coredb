from __future__ import annotations

import logging
import os
import re
import socket
import ssl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic.autogenerate import produce_migrations
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg import connect
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

from coredb.schema import load_metadata


ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("finsights_coredb")
MYSQL_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "DROP"}
POSTGRES_PRIVILEGES = {"CONNECT", "CREATE", "TEMPORARY"}


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


def _invoke_operations(operations: Operations, operations_list: list[Any]) -> None:
    for operation in operations_list:
        nested_operations = getattr(operation, "ops", None)
        if nested_operations is not None:
            _invoke_operations(operations, nested_operations)
        else:
            operations.invoke(operation)


@contextmanager
def _mysql_tls_hostname_resolution(connection: dict[str, Any]):
    tls_hostname = connection.get("tls_server_name")
    network_host = os.getenv(connection.get("host_env", "")) or connection.get("host", "")
    if not tls_hostname or tls_hostname == network_host:
        yield
        return

    original_getaddrinfo = socket.getaddrinfo

    def mapped_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any):
        resolved_host = network_host if host == tls_hostname else host
        return original_getaddrinfo(resolved_host, port, *args, **kwargs)

    socket.getaddrinfo = mapped_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def _url(connection: dict[str, Any], database: str | None = None, engine_name: str | None = None) -> URL:
    engine_name = engine_name or "mysql"
    host = os.getenv(connection.get("host_env", "")) or connection.get("host", "")
    if engine_name == "mysql" and connection.get("tls_server_name"):
        host = connection["tls_server_name"]
    port = int(os.getenv(connection.get("port_env", "")) or connection.get("port", 3306))
    username = connection.get("username") or _required_env(connection["username_env"])
    password = _required_env(connection["password_env"])
    db = database or connection.get("database")
    if engine_name == "postgresql":
        return URL.create(
            "postgresql+psycopg",
            username=username,
            password=password,
            host=host,
            port=port,
            database=db,
            query={"sslmode": connection.get("sslmode", "prefer"), "connect_timeout": str(connection.get("connect_timeout", 10))},
        )
    return URL.create("mysql+pymysql", username=username, password=password, host=host, port=port, database=db, query={"charset": "utf8mb4"})


def _engine(manifest: dict[str, Any], database: str | None = None) -> Engine:
    engine_name = manifest["engine"]
    url = _url(manifest["connection"], database=database, engine_name=engine_name)
    connect_args: dict[str, Any] = {"connect_timeout": int(manifest["connection"].get("connect_timeout", 10))}
    if engine_name == "mysql" and manifest["connection"].get("tls") == "verify":
        context = ssl.create_default_context()
        ca_pem = os.getenv(manifest["connection"].get("tls_ca_pem_env", ""))
        ca_file = os.getenv(manifest["connection"].get("tls_ca_file_env", "")) or manifest["connection"].get("tls_ca_file", "")
        if ca_pem:
            context.load_verify_locations(cadata=ca_pem)
        elif ca_file:
            ca_path = Path(ca_file)
            if not ca_path.is_absolute():
                ca_path = ROOT / ca_path
            context.load_verify_locations(cafile=str(ca_path))
        else:
            raise ValueError("a MySQL TLS CA PEM or CA file path is required")
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connect_args["ssl"] = context
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, hide_parameters=True)


def _mysql_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid MySQL identifier {value!r}")
    return "`" + value + "`"


def _mysql_account(user: str, host: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", user):
        raise ValueError(f"invalid MySQL username {user!r}")
    if not re.fullmatch(r"[%A-Za-z0-9_.:-]+", host):
        raise ValueError(f"invalid MySQL account host {host!r}")
    # PyMySQL treats percent signs in a statement as interpolation markers.
    sql_host = host.replace("%", "%%")
    return "'" + user.replace("'", "''") + "'@'" + sql_host.replace("'", "''") + "'"


def _mysql_drop_missing_databases(conn: Any, manifest: dict[str, Any]) -> None:
    prefix = manifest["managed_database_prefix"]
    desired = {item["name"] for item in manifest.get("databases", [])}
    actual = {row[0] for row in conn.exec_driver_sql("SHOW DATABASES").all()}
    for name in sorted(actual):
        if name.startswith(prefix) and name not in desired:
            LOG.warning("Dropping managed database %s", name)
            conn.exec_driver_sql(f"DROP DATABASE {_mysql_identifier(name)}")


def _mysql_sync_users(conn: Any, manifest: dict[str, Any]) -> None:
    configured = manifest.get("users", [])
    desired = {(user["name"], user.get("host", "%")): user for user in configured}
    prefix = manifest["managed_user_prefix"]
    existing = conn.exec_driver_sql("SELECT User, Host FROM mysql.user").all()
    for name, host in sorted(existing):
        if name.startswith(prefix) and (name, host) not in desired:
            LOG.warning("Dropping managed MySQL account %s@%s", name, host)
            conn.exec_driver_sql(f"DROP USER IF EXISTS {_mysql_account(name, host)}")

    for (name, host), user in desired.items():
        password = _required_env(user["password_env"])
        database = user["database"]
        privileges = sorted({value.upper() for value in user.get("privileges", [])})
        if not privileges or not set(privileges).issubset(MYSQL_PRIVILEGES):
            raise ValueError(f"unsupported MySQL privileges for {name}")
        account = _mysql_account(name, host)
        password_literal = conn.connection.driver_connection.escape(password)
        conn.exec_driver_sql(f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY {password_literal} REQUIRE SSL")
        conn.exec_driver_sql(f"ALTER USER {account} IDENTIFIED BY {password_literal} REQUIRE SSL")
        grant = ", ".join(privileges)
        conn.exec_driver_sql(f"GRANT {grant} ON {_mysql_identifier(database)}.* TO {account}")


def _mysql_apply(manifest: dict[str, Any]) -> None:
    admin = _engine(manifest)
    desired_databases = {item["name"] for item in manifest.get("databases", [])}
    try:
        with admin.begin() as conn:
            for database in sorted(desired_databases):
                conn.exec_driver_sql(
                    f"CREATE DATABASE IF NOT EXISTS {_mysql_identifier(database)} "
                    f"CHARACTER SET {manifest['database_charset']} COLLATE {manifest['database_collation']}"
                )

        for spec in manifest.get("databases", []):
            schema_paths = [(ROOT / manifest["target"] / value).resolve() for value in spec.get("schemas", [])]
            metadata = load_metadata(schema_paths)
            engine = _engine(manifest, database=spec["name"])
            try:
                with engine.begin() as conn:
                    context = MigrationContext.configure(
                        conn,
                        opts={
                            "compare_type": True,
                            "compare_server_default": True,
                            "include_object": lambda obj, name, kind, reflected, compare_to: not (
                                kind == "table" and reflected and compare_to is None and name == "alembic_version"
                            ),
                        },
                    )
                    migration = produce_migrations(context, metadata)
                    if migration.upgrade_ops.is_empty():
                        LOG.info("No schema changes for %s", spec["name"])
                    else:
                        LOG.info("Applying schema changes to %s", spec["name"])
                        operations = Operations(context)
                        _invoke_operations(operations, migration.upgrade_ops.ops)
            finally:
                engine.dispose()

        with admin.begin() as conn:
            _mysql_sync_users(conn, manifest)
            _mysql_drop_missing_databases(conn, manifest)
    finally:
        admin.dispose()


def _pg_literal(value: str) -> sql.Literal:
    return sql.Literal(value)


def _pg_identifier(value: str) -> sql.Identifier:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid PostgreSQL identifier {value!r}")
    return sql.Identifier(value)


def _pg_exec(conn: Any, statement: Any) -> None:
    raw = conn.connection.driver_connection
    with raw.cursor() as cursor:
        cursor.execute(statement)


def _pg_apply(manifest: dict[str, Any]) -> None:
    admin = _engine(manifest)
    users = {item["name"]: item for item in manifest.get("users", [])}
    database_specs = {item["name"]: item for item in manifest.get("databases", [])}
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for name, user in users.items():
                password = _required_env(user["password_env"])
                LOG.info("Ensuring PostgreSQL role %s", name)
                exists = conn.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).first()
                role = _pg_identifier(name)
                if exists:
                    _pg_exec(conn, sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role, _pg_literal(password)))
                else:
                    _pg_exec(conn, sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(role, _pg_literal(password)))
                marker = manifest["managed_role_comment_prefix"] + name
                _pg_exec(conn, sql.SQL("COMMENT ON ROLE {} IS {}").format(role, _pg_literal(marker)))

            for name, spec in database_specs.items():
                database = _pg_identifier(name)
                exists = conn.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).first()
                owner_name = spec.get("owner")
                owner_name = owner_name if owner_name in users else manifest["connection"]["username"]
                LOG.info("Ensuring PostgreSQL database %s owned by %s", name, owner_name)
                owner = _pg_identifier(owner_name)
                if exists:
                    _pg_exec(conn, sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, owner))
                else:
                    _pg_exec(conn, sql.SQL("CREATE DATABASE {} OWNER {}").format(database, owner))
                _pg_exec(conn, sql.SQL("COMMENT ON DATABASE {} IS {}").format(database, _pg_literal(manifest["managed_database_comment"])))

            marked_databases = conn.exec_driver_sql(
                "SELECT datname FROM pg_database WHERE shobj_description(oid, 'pg_database') = %s",
                (manifest["managed_database_comment"],),
            ).all()
            for (name,) in marked_databases:
                if name in database_specs:
                    continue
                database = _pg_identifier(name)
                LOG.warning("Dropping managed PostgreSQL database %s", name)
                conn.exec_driver_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
                _pg_exec(conn, sql.SQL("DROP DATABASE {}").format(database))

        for name, spec in database_specs.items():
            schema_paths = [(ROOT / manifest["target"] / value).resolve() for value in spec.get("schemas", [])]
            metadata = load_metadata(schema_paths, engine_name="postgresql")
            engine = _engine(manifest, database=name)
            try:
                with engine.begin() as conn:
                    for old_owner_name in spec.get("reassign_public_tables_from", []):
                        if old_owner_name == spec.get("owner"):
                            continue
                        LOG.info(
                            "Reassigning PostgreSQL public tables in %s from %s to %s",
                            name,
                            old_owner_name,
                            spec["owner"],
                        )
                    for role_spec in users.values():
                        if name not in role_spec.get("databases", []):
                            continue
                        privileges = sorted({value.upper() for value in role_spec.get("privileges", [])})
                        if not set(privileges).issubset(POSTGRES_PRIVILEGES):
                            raise ValueError(f"unsupported PostgreSQL privileges for {role_spec['name']}")
                        role = _pg_identifier(role_spec["name"])
                        if privileges:
                            _pg_exec(conn, sql.SQL("GRANT {} ON DATABASE {} TO {}").format(
                                sql.SQL(", ").join(sql.SQL(value) for value in privileges),
                                _pg_identifier(name),
                                role,
                            ))
                            _pg_exec(conn, sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(role))

                    owner = _pg_identifier(spec["owner"])
                    for old_owner_name in spec.get("reassign_public_tables_from", []):
                        tables = conn.exec_driver_sql(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = %s",
                            (old_owner_name,),
                        ).all()
                        for (table_name,) in tables:
                            _pg_exec(
                                conn,
                                sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                                    sql.Identifier("public"),
                                    sql.Identifier(table_name),
                                    owner,
                                ),
                            )

                        sequences = conn.exec_driver_sql(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "JOIN pg_roles r ON r.oid = c.relowner "
                            "WHERE c.relkind = 'S' AND n.nspname = 'public' AND r.rolname = %s",
                            (old_owner_name,),
                        ).all()
                        for (sequence_name,) in sequences:
                            _pg_exec(
                                conn,
                                sql.SQL("ALTER SEQUENCE {}.{} OWNER TO {}").format(
                                    sql.Identifier("public"),
                                    sql.Identifier(sequence_name),
                                    owner,
                                ),
                            )

                    if schema_paths:
                        def include_object(obj: Any, object_name: str, kind: str, reflected: bool, compare_to: Any) -> bool:
                            if kind == "table" and reflected:
                                if object_name in {"alembic_version", "_prisma_migrations"}:
                                    return False
                                if compare_to is None and not spec.get("drop_missing_tables", True):
                                    return False
                            return True

                        context = MigrationContext.configure(
                            conn,
                            opts={
                                "compare_type": True,
                                "compare_server_default": True,
                                "include_object": include_object,
                            },
                        )
                        migration = produce_migrations(context, metadata)
                        if migration.upgrade_ops.is_empty():
                            LOG.info("No schema changes for PostgreSQL database %s", name)
                        else:
                            LOG.info("Applying schema changes to PostgreSQL database %s", name)
                            operations = Operations(context)
                            _invoke_operations(operations, migration.upgrade_ops.ops)

                    for role_spec in users.values():
                        if name not in role_spec.get("databases", []) or not schema_paths:
                            continue
                        role = _pg_identifier(role_spec["name"])
                        _pg_exec(conn, sql.SQL(
                            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                        ).format(role))
                        _pg_exec(conn, sql.SQL(
                            "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                        ).format(role))
                        admin_role = _pg_identifier(manifest["connection"].get("username", "postgres"))
                        _pg_exec(conn, sql.SQL(
                            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                        ).format(admin_role, role))
                        _pg_exec(conn, sql.SQL(
                            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                            "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                        ).format(admin_role, role))

            finally:
                engine.dispose()

        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            marked_roles = conn.exec_driver_sql(
                "SELECT r.rolname FROM pg_roles r WHERE shobj_description(r.oid, 'pg_authid') LIKE %s",
                (manifest["managed_role_comment_prefix"] + "%",),
            ).all()
            for (name,) in marked_roles:
                if name in users:
                    continue
                role = _pg_identifier(name)
                LOG.warning("Dropping managed PostgreSQL role %s", name)
                for database in database_specs:
                    engine = _engine(manifest, database=database)
                    try:
                        with engine.begin() as db_conn:
                            _pg_exec(db_conn, sql.SQL("REASSIGN OWNED BY {} TO {}").format(role, _pg_identifier(manifest["connection"]["username"])))
                            _pg_exec(db_conn, sql.SQL("DROP OWNED BY {}").format(role))
                    finally:
                        engine.dispose()
                conn.exec_driver_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s AND pid <> pg_backend_pid()", (name,))
                _pg_exec(conn, sql.SQL("DROP ROLE {}").format(role))

        host = os.getenv(manifest["connection"].get("host_env", "")) or manifest["connection"].get("host", "")
        port = int(os.getenv(manifest["connection"].get("port_env", "")) or manifest["connection"].get("port", 5432))
        for user in users.values():
            for database in user.get("databases", []):
                LOG.info("Verifying PostgreSQL login for %s on %s", user["name"], database)
                with connect(
                    host=host,
                    port=port,
                    dbname=database,
                    user=user["name"],
                    password=_required_env(user["password_env"]),
                    sslmode=manifest["connection"].get("sslmode", "prefer"),
                    connect_timeout=int(manifest["connection"].get("connect_timeout", 10)),
                ):
                    pass
    finally:
        admin.dispose()


def apply_target(manifest: dict[str, Any]) -> None:
    _validate_target(manifest)
    if manifest["engine"] == "mysql":
        with _mysql_tls_hostname_resolution(manifest["connection"]):
            _mysql_apply(manifest)
    elif manifest["engine"] == "postgresql":
        _pg_apply(manifest)
    else:
        raise ValueError(f"unsupported engine {manifest['engine']!r}")


def _validate_target(manifest: dict[str, Any]) -> None:
    connection = manifest["connection"]
    _required_env(connection["password_env"])
    databases = manifest.get("databases", [])
    database_names = [item["name"] for item in databases]
    if len(database_names) != len(set(database_names)):
        raise ValueError("database names must be unique within a target")
    configured_users = manifest.get("users", [])
    user_keys = [(item["name"], item.get("host", "%")) for item in configured_users]
    if len(user_keys) != len(set(user_keys)):
        raise ValueError("user accounts must be unique within a target")
    known_databases = set(database_names)

    if manifest["engine"] == "mysql":
        prefix = manifest["managed_database_prefix"]
        user_prefix = manifest["managed_user_prefix"]
        if connection.get("username", "").startswith(user_prefix):
            raise ValueError("managed MySQL user prefix must not include the administrator account")
        if connection.get("tls") != "verify":
            raise ValueError("MySQL connections must verify TLS certificates")
        if not re.fullmatch(r"[A-Za-z0-9_]+", manifest["database_charset"]):
            raise ValueError("invalid MySQL database charset")
        if not re.fullmatch(r"[A-Za-z0-9_]+", manifest["database_collation"]):
            raise ValueError("invalid MySQL database collation")
        for name in database_names:
            if not name.startswith(prefix):
                raise ValueError(f"managed MySQL database {name!r} must start with {prefix!r}")
        if not (os.getenv(connection.get("tls_ca_pem_env", "")) or os.getenv(connection.get("tls_ca_file_env", "")) or connection.get("tls_ca_file")):
            raise ValueError("a MySQL TLS CA PEM or CA file path is required")
        for spec in databases:
            paths = [(ROOT / manifest["target"] / value).resolve() for value in spec.get("schemas", [])]
            metadata = load_metadata(paths)
            metadata.sorted_tables
        for user in configured_users:
            if not user["name"].startswith(user_prefix):
                raise ValueError(f"managed MySQL user {user['name']!r} must start with {user_prefix!r}")
            if user["database"] not in known_databases:
                raise ValueError(f"MySQL user {user['name']!r} refers to an unmanaged database")
            if user.get("host", "%") != "%":
                raise ValueError("this repo currently supports MySQL application accounts on host '%'")
            _required_env(user["password_env"])
            privileges = {value.upper() for value in user.get("privileges", [])}
            if not privileges or not privileges.issubset(MYSQL_PRIVILEGES):
                raise ValueError(f"unsupported MySQL privileges for {user['name']}")
    elif manifest["engine"] == "postgresql":
        configured_user_names = {user["name"] for user in configured_users}
        if len(configured_user_names) != len(configured_users):
            raise ValueError("PostgreSQL role names must be unique within a target")
        for user in configured_users:
            _required_env(user["password_env"])
            if not set(user.get("databases", [])).issubset(known_databases):
                raise ValueError(f"PostgreSQL user {user['name']!r} refers to an unmanaged database")
            privileges = {value.upper() for value in user.get("privileges", [])}
            if not privileges.issubset(POSTGRES_PRIVILEGES):
                raise ValueError(f"unsupported PostgreSQL privileges for {user['name']}")
        for database in databases:
            paths = [(ROOT / manifest["target"] / value).resolve() for value in database.get("schemas", [])]
            load_metadata(paths, engine_name="postgresql")
            owner = database.get("owner")
            if owner and owner not in configured_user_names:
                LOG.warning("Database %s will use the administrator as owner because %s is not configured", database["name"], owner)
