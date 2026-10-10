"""数据库连接与迁移。SQLite 开 WAL，web 和 worker 两个进程可以同时读写。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = make_engine(url)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def backup(self, dest: Path, keep: int = 14) -> Path:
        """SQLite 在线备份（服务运行中也可以执行），只保留最近 keep 份。"""
        if not self.url.startswith("sqlite:///"):
            raise ValueError("只支持 SQLite；Postgres 请用 pg_dump")
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"workbench-{datetime.now():%Y%m%d-%H%M%S}.db"
        with sqlite3.connect(self.url.removeprefix("sqlite:///")) as src, sqlite3.connect(target) as out:
            src.backup(out)
        for old in sorted(dest.glob("workbench-*.db"))[:-keep]:
            old.unlink()
        return target

    def migrate(self) -> None:
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.attributes["connection_url"] = self.url
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
