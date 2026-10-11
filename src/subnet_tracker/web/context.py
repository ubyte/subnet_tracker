import sqlite3

from flask import Flask, current_app, g

from subnet_tracker.config import Config
from subnet_tracker.core.db import connect

_CONFIG = "subnet_tracker.config"
_DB = "db"


def install_config(app: Flask, config: Config) -> None:
    app.extensions[_CONFIG] = config


def app_config() -> Config:
    config = current_app.extensions.get(_CONFIG)
    if not isinstance(config, Config):
        raise RuntimeError("the web app has no Config")
    return config


def request_db() -> sqlite3.Connection:
    conn: object = getattr(g, _DB, None)
    if not isinstance(conn, sqlite3.Connection):
        conn = connect(app_config().db)
        setattr(g, _DB, conn)
    return conn


def close_request_db(_error: BaseException | None) -> None:
    conn: object = g.pop(_DB, None)
    if isinstance(conn, sqlite3.Connection):
        conn.close()
