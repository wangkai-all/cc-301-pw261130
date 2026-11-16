"""SQLite 存储层：所有表均为追加式（append-only），历史报告永不改写。

版本语义：
- batch：一次入库的输入文件（事件 / 分组 / 身份归并 / 曝光修订），不可变。
- snapshot：实验的"输入版本"，定义为 batch_id <= through_batch 的全部输入。
- report：对某个 snapshot 用固定参数的一次完整计算，结果写入 sections，之后只读。
"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments(
  experiment_id TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches(
  batch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  kind        TEXT NOT NULL,              -- events | assignments | merges | revisions
  source      TEXT NOT NULL,              -- 源文件名
  sha256      TEXT NOT NULL UNIQUE,       -- 同一文件重复入库会被拒绝（幂等）
  n_rows      INTEGER NOT NULL,
  n_accepted  INTEGER NOT NULL DEFAULT 0,
  n_rejected  INTEGER NOT NULL DEFAULT 0,
  detail      TEXT NOT NULL DEFAULT '{}', -- JSON：数据质量计数
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments(
  experiment_id TEXT NOT NULL,
  merchant_id   TEXT NOT NULL,
  arm           TEXT NOT NULL,
  batch_id      INTEGER NOT NULL,
  PRIMARY KEY(experiment_id, merchant_id, batch_id)
);

CREATE TABLE IF NOT EXISTS events(
  event_uid    TEXT PRIMARY KEY,          -- 幂等键：重复 event_uid 直接拒绝
  batch_id     INTEGER NOT NULL,
  experiment_id TEXT NOT NULL,
  event_type   TEXT NOT NULL,             -- exposure | conversion | refund
  user_id      TEXT NOT NULL,
  merchant_id  TEXT NOT NULL,
  ts           TEXT NOT NULL,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  order_id     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_exp ON events(experiment_id, batch_id);

CREATE TABLE IF NOT EXISTS merges(
  merge_id      TEXT PRIMARY KEY,
  batch_id      INTEGER NOT NULL,
  experiment_id TEXT NOT NULL,
  primary_user  TEXT NOT NULL,
  secondary_user TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions(
  revision_id   TEXT PRIMARY KEY,
  batch_id      INTEGER NOT NULL,
  experiment_id TEXT NOT NULL,
  event_uid     TEXT NOT NULL,
  new_merchant_id TEXT NOT NULL,
  reason        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots(
  snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  through_batch INTEGER NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports(
  report_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  snapshot_id  INTEGER NOT NULL,
  seed         INTEGER NOT NULL,
  n_boot       INTEGER NOT NULL,
  code_version TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  error        TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS sections(
  report_id INTEGER NOT NULL,
  section   TEXT NOT NULL,
  json      TEXT NOT NULL,
  PRIMARY KEY(report_id, section)
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path):
    d = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(d, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def ensure_experiment(conn, experiment_id, name=None):
    row = conn.execute(
        "SELECT experiment_id FROM experiments WHERE experiment_id=?",
        (experiment_id,)).fetchone()
    if row:
        return
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO experiments(experiment_id, name, created_at) VALUES(?,?,?)",
        (experiment_id, name or experiment_id,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
