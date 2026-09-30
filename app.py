import os
import logging
import threading
from pathlib import Path
from time import monotonic
from typing import Any

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
PGHOST = os.environ.get("PGHOST", "")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGDATABASE = os.environ.get("PGDATABASE", "databricks_postgres")
PGSSLMODE = os.environ.get("PGSSLMODE", "require")
MAX_RESULT_ROWS = 500
_endpoint_path = os.environ.get("LAKEBASE_ENDPOINT", "")
_endpoint_lock = threading.Lock()
_local_clients: dict[tuple[str, str], WorkspaceClient] = {}
_local_clients_lock = threading.Lock()
app = FastAPI(title="Lakebase SQL Workbench", docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)


class UserContext:
    def __init__(self, client: WorkspaceClient, username: str):
        self.client = client
        self.username = username


class QueryRequest(BaseModel):
    database: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=100_000)
    limit: int | None = Field(default=500, ge=1, le=100_000)


def local_workspace_client(host: str, profile: str | None) -> WorkspaceClient:
    key = (host, profile or "")
    with _local_clients_lock:
        if key not in _local_clients:
            _local_clients[key] = WorkspaceClient(host=host, profile=profile)
        return _local_clients[key]


def require_user(request: Request) -> UserContext:
    host = os.environ.get("DATABRICKS_HOST")
    if not host:
        raise HTTPException(status_code=503, detail="DATABRICKS_HOST is not configured.")
    token = request.headers.get("X-Forwarded-Access-Token")
    is_databricks_app = bool(os.environ.get("DATABRICKS_APP_NAME"))
    auth_source = "forwarded user token" if token else "local credentials"
    auth_stage = "workspace client initialization"
    try:
        if token:
            client = WorkspaceClient(host=host, token=token, auth_type="pat")
        elif is_databricks_app:
            raise HTTPException(
                status_code=401,
                detail="Databricks Apps did not provide x-forwarded-access-token. Enable user authorization for this app.",
            )
        else:
            token = os.environ.get("DATABRICKS_TOKEN")
            if token:
                auth_source = "local token"
                client = WorkspaceClient(host=host, token=token)
            else:
                profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
                auth_source = "local OAuth profile"
                client = local_workspace_client(host, profile)
            auth_stage = "current user lookup"
        username = client.current_user.me().user_name
    except HTTPException:
        raise
    except Exception as exc:
        error_status = getattr(exc, "status", None)
        error_code = getattr(exc, "error_code", None)
        error_message = str(exc)
        if token:
            error_message = error_message.replace(token, "[redacted]")
        error_message = error_message[:300]
        logger.warning(
            "Databricks user auth failed source=%s app_runtime=%s stage=%s error_type=%s status=%s error_code=%s detail=%s",
            auth_source,
            is_databricks_app,
            auth_stage,
            type(exc).__name__,
            error_status,
            error_code,
            error_message,
        )
        if auth_source == "forwarded user token":
            detail = f"Could not authenticate x-forwarded-access-token (stage={auth_stage}, type={type(exc).__name__}, status={error_status}, code={error_code}): {error_message}"
        else:
            detail = f"Could not authenticate local Databricks credentials (stage={auth_stage}, type={type(exc).__name__}, status={error_status}, code={error_code}): {error_message}"
        raise HTTPException(
            status_code=401,
            detail=detail,
        ) from exc
    if not username:
        raise HTTPException(status_code=401, detail="Databricks did not return a database username.")
    return UserContext(client, username)


def lakebase_endpoint(user: UserContext) -> str:
    global _endpoint_path
    if _endpoint_path:
        return _endpoint_path
    if not PGHOST:
        raise HTTPException(status_code=503, detail="PGHOST is not configured.")
    with _endpoint_lock:
        if _endpoint_path:
            return _endpoint_path
        try:
            expected = PGHOST.rstrip(".").lower()
            for project in user.client.postgres.list_projects():
                for branch in user.client.postgres.list_branches(parent=project.name):
                    for endpoint in user.client.postgres.list_endpoints(parent=branch.name):
                        endpoint_host = endpoint.status.hosts.host
                        if endpoint_host and endpoint_host.rstrip(".").lower() == expected:
                            _endpoint_path = endpoint.name
                            return _endpoint_path
        except Exception as exc:
            raise HTTPException(status_code=403, detail=f"Cannot discover the Lakebase endpoint: {exc}") from exc
    raise HTTPException(status_code=404, detail="No Lakebase endpoint matching PGHOST is visible to this user.")


def connect(database: str, user: UserContext) -> psycopg.Connection[Any]:
    if not PGHOST:
        raise HTTPException(status_code=503, detail="PGHOST is not configured.")
    try:
        password = user.client.postgres.generate_database_credential(endpoint=lakebase_endpoint(user)).token
        return psycopg.connect(
            host=PGHOST, port=PGPORT, dbname=database, user=user.username,
            password=password, sslmode=PGSSLMODE, connect_timeout=10,
            autocommit=True, row_factory=dict_row,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Lakebase connection failed: {exc}") from exc


def fetch(database: str, user: UserContext, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect(database, user) as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@app.get("/api/whoami")
def whoami(user: UserContext = Depends(require_user)) -> dict[str, str]:
    return {"username": user.username}


@app.get("/api/databases")
def databases(user: UserContext = Depends(require_user)) -> dict[str, Any]:
    rows = fetch(PGDATABASE, user, "SELECT datname AS name FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname")
    return {"databases": rows, "default": PGDATABASE}


@app.get("/api/schemas")
def schemas(database: str, user: UserContext = Depends(require_user)) -> dict[str, Any]:
    rows = fetch(database, user, "SELECT nspname AS name FROM pg_namespace ORDER BY nspname")
    return {"schemas": rows}


@app.get("/api/objects")
def objects(database: str, schema: str, show_system: bool = False, user: UserContext = Depends(require_user)) -> dict[str, Any]:
    rows = fetch(database, user, """
        SELECT kind, name, identity_args FROM (
          SELECT CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table'
            WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'f' THEN 'foreign table'
            WHEN 'S' THEN 'sequence' WHEN 'i' THEN 'index' WHEN 'I' THEN 'partitioned index' END AS kind,
            c.relname AS name, ''::text AS identity_args
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','f','S','i','I')
          UNION ALL
          SELECT CASE p.prokind WHEN 'p' THEN 'procedure' WHEN 'a' THEN 'aggregate' ELSE 'function' END,
            p.proname, pg_get_function_identity_arguments(p.oid)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
          WHERE n.nspname = %s AND p.prokind IN ('f','p','a','w')
          UNION ALL
          SELECT CASE t.typtype WHEN 'e' THEN 'enum' WHEN 'd' THEN 'domain'
            WHEN 'c' THEN 'composite type' ELSE 'type' END, t.typname, ''::text
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
          WHERE n.nspname = %s AND t.typtype IN ('e','d','c','r') AND t.typelem = 0 AND t.typname NOT LIKE '\\_%%'
          UNION ALL
          SELECT 'trigger', t.tgname, c.relname FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = %s AND NOT t.tgisinternal
        ) catalog_objects
        WHERE kind IS NOT NULL AND (%s OR (%s NOT IN ('pg_catalog','information_schema') AND %s NOT LIKE 'pg_toast%%'))
        ORDER BY kind, name, identity_args
    """, (schema, schema, schema, schema, show_system, schema, schema))
    return {"objects": rows}


@app.get("/api/object")
def object_detail(
    database: str, schema: str, name: str, kind: str, identity_args: str = "",
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    qualified = f"{ident(schema)}.{ident(name)}"
    result: dict[str, Any] = {"schema": schema, "name": name, "kind": kind, "definition": "", "columns": [], "indexes": [], "constraints": []}
    if kind in {"table", "partitioned table", "foreign table", "view", "materialized view"}:
        columns = fetch(database, user, """
          SELECT a.attname AS name, format_type(a.atttypid,a.atttypmod) AS data_type,
            NOT a.attnotnull AS nullable, pg_get_expr(d.adbin,d.adrelid) AS default_value,
            a.attidentity AS identity, a.attgenerated AS generated
          FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
          WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
        """, (schema, name))
        constraints = fetch(database, user, """
          SELECT conname AS name, pg_get_constraintdef(c.oid,true) AS definition FROM pg_constraint c
          JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
          WHERE n.nspname=%s AND r.relname=%s ORDER BY conname
        """, (schema, name))
        indexes = fetch(database, user, "SELECT indexname AS name,indexdef AS definition FROM pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname", (schema, name))
        result.update(columns=columns, constraints=constraints, indexes=indexes)
        if kind in {"view", "materialized view"}:
            rows = fetch(database, user, "SELECT pg_get_viewdef(c.oid,true) AS body FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s", (schema, name))
            prefix = "CREATE OR REPLACE VIEW" if kind == "view" else "CREATE MATERIALIZED VIEW"
            result["definition"] = f"{prefix} {qualified} AS\n{rows[0]['body'] if rows else ''};"
        else:
            definitions = []
            for column in columns:
                line = f"  {ident(column['name'])} {column['data_type']}"
                if column["identity"]:
                    line += " GENERATED " + ("ALWAYS" if column["identity"] == "a" else "BY DEFAULT") + " AS IDENTITY"
                elif column["generated"]:
                    line += f" GENERATED ALWAYS AS ({column['default_value']}) STORED"
                else:
                    if column["default_value"]:
                        line += f" DEFAULT {column['default_value']}"
                    if not column["nullable"]:
                        line += " NOT NULL"
                definitions.append(line)
            definitions.extend(f"  CONSTRAINT {ident(item['name'])} {item['definition']}" for item in constraints)
            definition = f"CREATE TABLE {qualified} (\n" + ",\n".join(definitions) + "\n);"
            if indexes:
                definition += "\n\n" + "\n".join(item["definition"] + ";" for item in indexes)
            result["definition"] = definition
    elif kind in {"function", "procedure", "aggregate"}:
        rows = fetch(database, user, "SELECT pg_get_functiondef(p.oid) AS definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s AND p.proname=%s AND pg_get_function_identity_arguments(p.oid)=%s LIMIT 1", (schema, name, identity_args))
        result["definition"] = rows[0]["definition"] if rows else "Definition is not available for this object."
    elif kind in {"index", "partitioned index"}:
        rows = fetch(database, user, "SELECT pg_get_indexdef(c.oid) AS definition FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s", (schema, name))
        result["definition"] = rows[0]["definition"] + ";" if rows else "Definition is not available for this object."
    elif kind == "trigger":
        rows = fetch(database, user, "SELECT pg_get_triggerdef(t.oid,true) AS definition,c.relname AS table_name FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND t.tgname=%s AND NOT t.tgisinternal", (schema, name))
        result["definition"] = rows[0]["definition"] + ";" if rows else "Definition is not available for this object."
        result["table"] = rows[0]["table_name"] if rows else ""
    elif kind in {"enum", "domain", "composite type", "type"}:
        rows = fetch(database, user, """
          SELECT t.typtype,format_type(t.typbasetype,t.typtypmod) AS base_type,e.enumlabel,
            a.attname AS field_name,format_type(a.atttypid,a.atttypmod) AS field_type
          FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
          LEFT JOIN pg_enum e ON e.enumtypid=t.oid LEFT JOIN pg_class r ON r.oid=t.typrelid
          LEFT JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum>0 AND NOT a.attisdropped
          WHERE n.nspname=%s AND t.typname=%s ORDER BY e.enumsortorder,a.attnum
        """, (schema, name))
        if kind == "enum":
            labels = ", ".join("'" + row["enumlabel"].replace("'", "''") + "'" for row in rows if row["enumlabel"] is not None)
            result["definition"] = f"CREATE TYPE {qualified} AS ENUM ({labels});"
        elif kind == "domain" and rows:
            result["definition"] = f"CREATE DOMAIN {qualified} AS {rows[0]['base_type']};"
        elif kind == "composite type":
            fields = [f"  {ident(row['field_name'])} {row['field_type']}" for row in rows if row["field_name"]]
            result["definition"] = f"CREATE TYPE {qualified} AS (\n" + ",\n".join(fields) + "\n);"
    elif kind == "sequence":
        rows = fetch(database, user, "SELECT start_value,min_value,max_value,increment_by,cycle FROM pg_sequences WHERE schemaname=%s AND sequencename=%s", (schema, name))
        if rows:
            row = rows[0]
            cycle = "CYCLE" if row["cycle"] else "NO CYCLE"
            result["definition"] = f"CREATE SEQUENCE {qualified} INCREMENT BY {row['increment_by']} MINVALUE {row['min_value']} MAXVALUE {row['max_value']} START WITH {row['start_value']} {cycle};"
    return result


@app.post("/api/query")
def run_query(body: QueryRequest, user: UserContext = Depends(require_user)) -> dict[str, Any]:
    statement = body.query.strip()
    if not statement:
        raise HTTPException(status_code=400, detail="Enter a SQL statement first.")
    started = monotonic()
    try:
        with connect(body.database, user) as connection, connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '30s'")
            cursor.execute(statement, prepare=False)
            columns = [item.name for item in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(body.limit) if columns and body.limit is not None else cursor.fetchall() if columns else []
            return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": body.limit is not None and len(rows) == body.limit, "command": cursor.statusmessage, "elapsed_ms": round((monotonic() - started) * 1000)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")))