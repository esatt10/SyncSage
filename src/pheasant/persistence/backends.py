"""Storage backends behind :class:`~pheasant.persistence.state_store.StateStore`.

Phase 35.2. The seam exists because one writer process per knowledge base is
pheasant's hard scaling ceiling (``sync/locks.py``'s ``EngineLease``), and
SQLite is why: it has no notion of a second process committing concurrently to
the same file beyond serialized writes. Postgres removes that constraint, so
35.4 can give each *source* its own lease and let N indexers commit at once.

**SQLite stays the default and stays byte-identical.** CLAUDE.md rule 7 —
standalone mode is sacred — is not satisfied by "SQLite still works if you
configure it"; it is satisfied by the SQLite path executing the same SQL over
the same connections it always did. So :class:`SqliteBackend` is the previous
``StateStore`` connection handling moved, not rewritten, and
:meth:`Dialect.translate` is a no-op for it.

Both backends hand back rows that support ``row["col"]`` *and* ``row[0]``,
because pheasant's call sites use both and quietly breaking the positional ones
is exactly the kind of failure that reaches production.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pheasant.persistence.sql import POSTGRES, SQLITE, Dialect, Row

logger = logging.getLogger(__name__)

#: How long a statement waits for a lock before giving up. Raised from 5s to
#: 60s during the 2026-08-12 scale run: real sync/API/scheduler contention at
#: corpus size hit "database is locked" well inside the old budget.
BUSY_TIMEOUT_MS = 60_000

#: Postgres equivalent, applied per session.
STATEMENT_TIMEOUT_MS = 0  # 0 = no limit; indexing commits can be long.


class StateBackend(ABC):
    """The database operations :class:`StateStore` needs.

    Deliberately narrow and statement-oriented rather than an ORM: the store
    above it is already a typed facade, and the value here is swapping the
    engine, not re-expressing the schema.
    """

    dialect: Dialect

    @abstractmethod
    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Row]:
        """Run a query and return every row."""

    @abstractmethod
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Run one statement."""

    @abstractmethod
    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> None:
        """Run one statement over many parameter tuples."""

    @abstractmethod
    def executescript(self, script: str) -> None:
        """Run a multi-statement DDL script. Used only by ``migrate``."""

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def table_columns(self, table: str) -> set[str]:
        """Column names of ``table``, or an empty set when it does not exist.

        Its own method because the two dialects answer it completely
        differently (``PRAGMA table_info`` vs ``information_schema``), and the
        one-shot additive migrations in the store need the answer.
        """

    @property
    def description(self) -> str:
        """Something safe to log. Never includes a password."""

        return self.dialect.name


class SqliteBackend(StateBackend):
    """One SQLite connection per thread.

    WAL mode is SQLite's sanctioned way to let multiple threads read the same
    database truly concurrently — but only when each thread uses its *own*
    connection. An earlier version shared one connection across every thread:
    the search paths (text/vector/graph) run in parallel, and overlapping
    execute()+fetch calls interleaved cursor state, handing back a Row missing
    an expected column. Locking around it was correct but threw away the
    concurrency WAL exists to provide (measured: a multi-query retrieve took
    22-29s where a single-query one over the same data took 1.3-2s).
    Thread-local connections remove the shared state instead of locking it.
    """

    dialect = SQLITE

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Every connection ever opened, so close() can close all of them — not
        # just whichever thread happens to call close().
        self._all_conns: list[sqlite3.Connection] = []
        self._registry_lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=True)
            conn.row_factory = sqlite3.Row
            # Crash/concurrency safety (Synapse step 21.2): WAL survives
            # kill -9 mid-write, busy_timeout rides out concurrent readers,
            # synchronous=NORMAL is the sanctioned WAL durability level.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._registry_lock:
                self._all_conns.append(conn)
        return conn

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Row]:
        # sqlite3.Row already satisfies the dual-access contract, so it is
        # returned as-is: converting would allocate on every read for nothing.
        return self.conn.execute(sql, params).fetchall()  # type: ignore[return-value]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> None:
        self.conn.executemany(sql, seq)

    def executescript(self, script: str) -> None:
        self.conn.executescript(script)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        with self._registry_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # pragma: no cover - already-closed is fine
                pass
        self._local = threading.local()

    def table_columns(self, table: str) -> set[str]:
        try:
            return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    @property
    def description(self) -> str:
        return f"sqlite:{self.path}"


class _PgResult:
    """What ``conn.execute(...)`` hands back, shaped like a sqlite3 cursor.

    ``StateStore`` iterates the result of ``execute`` in places and calls
    ``fetchone()`` in others, so the adapter has to be both. Rows are
    materialized eagerly because the cursor is closed as soon as the statement
    finishes — a lazy cursor handed back to a caller that iterates it later is
    how you get "cursor already closed" at a random call site.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list[Row]):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self) -> list[Row]:
        return list(self._rows)

    def fetchone(self) -> Row | None:
        return self._rows[0] if self._rows else None


class _PgConnection:
    """A ``sqlite3.Connection``-shaped view over a pooled Postgres connection.

    ``StateStore`` reaches for ``self.conn`` in **76 places**, including ten
    ``with self.conn:`` transaction blocks. Rewriting every one of them was the
    alternative; this is one class instead, and — decisively — it leaves the
    SQLite path executing literally unchanged code rather than 76 freshly
    edited lines on the default path.

    ``with conn:`` matches sqlite3's semantics: commit on clean exit, roll back
    on exception. Getting that backwards would turn a failed sync into a
    half-applied one.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: PostgresBackend):
        self._backend = backend

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _PgResult:
        return _PgResult(self._backend.rows(sql, params))

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> None:
        self._backend.executemany(sql, seq)

    def executescript(self, script: str) -> None:
        self._backend.executescript(script)

    def commit(self) -> None:
        self._backend.commit()

    def rollback(self) -> None:
        self._backend._conn().rollback()

    def __enter__(self) -> _PgConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class PostgresBackend(StateBackend):
    """A pooled Postgres connection, one checked out per thread in use.

    Uses ``psycopg`` 3 with a connection pool rather than a connection per
    thread: unlike a SQLite file handle, a Postgres connection is a server-side
    process, so one per thread across an API server's threadpool plus the
    search fan-out would open dozens for a workload that needs a handful.

    Autocommit is **off**: the indexing path relies on transactional commits
    (``replace_artifact_chunks`` deletes then re-inserts, and a crash between
    the two must not leave an artifact with no chunks). ``commit()`` is
    explicit, exactly as with SQLite.
    """

    dialect = POSTGRES

    def __init__(self, dsn: str, *, pool_size: int = 10, application_name: str = "pheasant"):
        try:
            from psycopg_pool import ConnectionPool
        except ModuleNotFoundError as exc:  # pragma: no cover - dep-gated
            raise RuntimeError(
                "storage.backend is 'postgres' but psycopg is not installed. "
                "Install pheasant with the postgres extra: pip install 'pheasant-kb[postgres]'"
            ) from exc

        self.dsn = dsn
        self._local = threading.local()
        # min_size=1 so a container that never queries does not hold a pool of
        # idle server processes; the pool grows on demand up to pool_size.
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max(1, int(pool_size)),
            kwargs={"application_name": application_name, "autocommit": False},
            open=True,
        )

    @property
    def conn(self) -> _PgConnection:
        """A sqlite3-shaped view, so ``StateStore.conn`` works unchanged."""

        adapter = getattr(self._local, "adapter", None)
        if adapter is None:
            adapter = _PgConnection(self)
            self._local.adapter = adapter
        return adapter

    def _conn(self):
        """The connection this thread is currently using.

        Checked out for the life of the thread's unit of work rather than per
        statement: ``StateStore`` calls ``execute`` several times and then
        ``commit``, and a per-statement checkout would put those in different
        transactions — turning one atomic artifact replacement into several.
        """

        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._pool.getconn()
            self._local.conn = conn
        return conn

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Row]:
        conn = self._conn()
        with conn.cursor() as cursor:
            cursor.execute(self.dialect.translate(sql), params or None)
            if cursor.description is None:
                return []
            columns = [column.name for column in cursor.description]
            return [Row(dict(zip(columns, values, strict=True))) for values in cursor.fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        conn = self._conn()
        with conn.cursor() as cursor:
            cursor.execute(self.dialect.translate(sql), params or None)

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> None:
        if not seq:
            return
        conn = self._conn()
        with conn.cursor() as cursor:
            cursor.executemany(self.dialect.translate(sql), seq)

    def executescript(self, script: str) -> None:
        conn = self._conn()
        with conn.cursor() as cursor:
            cursor.execute(script)
        conn.commit()

    def commit(self) -> None:
        self._conn().commit()

    def close(self) -> None:
        self.release()
        try:
            self._pool.close()
        except Exception:  # pragma: no cover
            pass

    def release(self) -> None:
        """Return this thread's connection to the pool without closing it.

        Rolls back first. Returning a connection still ``INTRANS`` makes the
        pool log a warning and, worse, leaves whatever the caller did *after*
        its last ``commit()`` pending on a connection someone else is about to
        use — the pool would roll it back for us, but at a moment we no longer
        control. Discarding uncommitted work explicitly is the honest close:
        anything that mattered was committed.
        """

        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - already-broken connection
            pass
        try:
            self._pool.putconn(conn)
        except Exception:  # pragma: no cover
            pass
        self._local.conn = None
        self._local.adapter = None

    def table_columns(self, table: str) -> set[str]:
        found = self.rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name=? AND table_schema=current_schema()",
            (table,),
        )
        return {str(row["column_name"]) for row in found}

    @property
    def description(self) -> str:
        # Never log the DSN: it carries the password.
        return "postgres"


def open_backend(
    path: str | Path | None = None,
    *,
    backend: str = "sqlite",
    dsn: str | None = None,
    pool_size: int = 10,
) -> StateBackend:
    """Build the configured backend.

    ``path`` is what every existing caller passes, and with the default
    ``backend`` it produces exactly the SQLite store they had before.
    """

    if str(backend or "sqlite").lower() == "postgres":
        if not dsn:
            raise ValueError(
                "storage.backend is 'postgres' but no DSN was resolved. Set "
                "storage.dsn_env to the name of an environment variable holding it."
            )
        return PostgresBackend(dsn, pool_size=pool_size)
    if path is None:
        raise ValueError("the sqlite backend needs a database path")
    return SqliteBackend(path)
