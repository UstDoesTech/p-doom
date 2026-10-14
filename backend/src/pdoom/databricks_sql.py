"""Run SQL on a Databricks SQL warehouse through the Statement Execution API.

The SDK is synchronous. Call SqlRunner from plain `def` FastAPI handlers, which run in
FastAPI's thread pool, never from inside `async def`.
"""

from __future__ import annotations

import re
import time
from typing import Any

_IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")


class SqlError(RuntimeError):
    pass


def quote_ident(name: str) -> str:
    """Backtick-quote one part of a Unity Catalog name. Names are used literally."""
    if not _IDENT.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return f"`{name}`"


class SqlRunner:
    def __init__(
        self,
        warehouse_id: str,
        catalog: str,
        schema: str,
        client: Any = None,
        max_wait_s: float = 60,
    ) -> None:
        self.warehouse_id = warehouse_id
        self.catalog = catalog
        self.schema = schema
        self.max_wait_s = max_wait_s
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient()
        return self._client

    def table(self, name: str) -> str:
        return ".".join(quote_ident(p) for p in (self.catalog, self.schema, name))

    def run(self, statement: str, params: dict[str, str] | None = None) -> list[list[str | None]]:
        """Execute one statement and return its rows as lists of strings."""
        from databricks.sdk.service.sql import StatementParameterListItem, StatementState

        items = [
            StatementParameterListItem(name=k, value=v, type="STRING")
            for k, v in (params or {}).items()
        ] or None
        api = self.client.statement_execution
        resp = api.execute_statement(
            warehouse_id=self.warehouse_id,
            statement=statement,
            parameters=items,
            wait_timeout="30s",
        )
        deadline = time.monotonic() + self.max_wait_s
        while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if time.monotonic() > deadline:
                api.cancel_execution(resp.statement_id)
                raise SqlError("Statement timed out")
            time.sleep(0.5)
            resp = api.get_statement(resp.statement_id)
        state = resp.status.state if resp.status else None
        if state != StatementState.SUCCEEDED:
            message = resp.status.error.message if resp.status and resp.status.error else state
            raise SqlError(f"Statement failed: {message}")
        return list(resp.result.data_array) if resp.result and resp.result.data_array else []
