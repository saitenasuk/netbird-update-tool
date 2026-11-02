"""SQLite 持久化：机器、脚本、任务、设置。

机器和脚本在界面上是一起编辑的（脚本用多行文本录入），所以 scripts 单独成表、
由 hosts 级联删除；任务只存摘要，逐行日志落在 data/jobs/<id>.log。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    group_name          TEXT    NOT NULL DEFAULT 'clients',
    address             TEXT    NOT NULL,
    ssh_user            TEXT    NOT NULL DEFAULT 'root',
    ssh_port            INTEGER NOT NULL DEFAULT 22,
    key_name            TEXT,
    detach              INTEGER NOT NULL DEFAULT 0,
    detach_log          TEXT    NOT NULL DEFAULT '/var/log/netbird-upgrade.log',
    detach_unit_pattern TEXT    NOT NULL DEFAULT 'netbird-upgrade-*',
    self_host           INTEGER NOT NULL DEFAULT 0,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    note                TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id     INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    path        TEXT    NOT NULL,
    label       TEXT    NOT NULL DEFAULT '',
    options     TEXT    NOT NULL DEFAULT '',
    default_all INTEGER NOT NULL DEFAULT 1,
    timeout     INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    spec        TEXT NOT NULL,
    result      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_OPTION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ValidationError(ValueError):
    """用户输入不合法，返回 400。"""


def parse_scripts(text: str) -> list[dict]:
    """把「路径 | 显示名 | 选项1,选项2」的多行文本解析成脚本列表。

    顺序即执行顺序；空行和以 # 开头的行忽略。
    """
    out: list[dict] = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        path = parts[0]
        if not path.startswith("/"):
            raise ValidationError(f"第 {lineno} 行：脚本路径必须是绝对路径（{path or '空'}）")
        if any(c in path for c in "\n\r\t"):
            raise ValidationError(f"第 {lineno} 行：路径里不能有控制字符")
        label = parts[1] if len(parts) > 1 and parts[1] else path.rsplit("/", 1)[-1]
        options_raw = parts[2] if len(parts) > 2 else ""
        options = [o.strip() for o in options_raw.split(",") if o.strip()]
        for opt in options:
            if not _OPTION_RE.match(opt):
                raise ValidationError(f"第 {lineno} 行：选项名 {opt!r} 只能用字母数字和 . _ -")
        out.append(
            {
                "path": path,
                "label": label,
                "options": options,
                "default_all": 1,
                "timeout": None,
            }
        )
    if not out:
        raise ValidationError("至少需要一个脚本")
    return out


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 机器 ----------

    def list_hosts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM hosts ORDER BY group_name, sort_order, id"
        ).fetchall()
        hosts = [dict(r) for r in rows]
        scripts = self._scripts_by_host()
        for h in hosts:
            h["scripts"] = scripts.get(h["id"], [])
            h["detach"] = bool(h["detach"])
            h["self_host"] = bool(h["self_host"])
        return hosts

    def _scripts_by_host(self) -> dict[int, list[dict]]:
        rows = self.conn.execute(
            "SELECT * FROM scripts ORDER BY host_id, position, id"
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for r in rows:
            s = dict(r)
            s["options"] = [o for o in s["options"].split(",") if o]
            grouped.setdefault(s["host_id"], []).append(s)
        return grouped

    def get_host(self, host_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
        if row is None:
            return None
        host = dict(row)
        host["scripts"] = self._scripts_by_host().get(host_id, [])
        host["detach"] = bool(host["detach"])
        host["self_host"] = bool(host["self_host"])
        return host

    def create_host(self, payload: dict) -> int:
        data = self._normalize_host(payload)
        cur = self.conn.execute(
            """INSERT INTO hosts (name, group_name, address, ssh_user, ssh_port, key_name,
                                  detach, detach_log, detach_unit_pattern, self_host,
                                  sort_order, note, created_at)
               VALUES (:name, :group_name, :address, :ssh_user, :ssh_port, :key_name,
                       :detach, :detach_log, :detach_unit_pattern, :self_host,
                       :sort_order, :note, :created_at)""",
            data,
        )
        host_id = int(cur.lastrowid)
        self._replace_scripts(host_id, payload.get("scripts_parsed") or [])
        self.conn.commit()
        return host_id

    def update_host(self, host_id: int, payload: dict) -> None:
        data = self._normalize_host(payload)
        data["id"] = host_id
        try:
            self.conn.execute(
                """UPDATE hosts SET name=:name, group_name=:group_name, address=:address,
                          ssh_user=:ssh_user, ssh_port=:ssh_port, key_name=:key_name,
                          detach=:detach, detach_log=:detach_log,
                          detach_unit_pattern=:detach_unit_pattern, self_host=:self_host,
                          sort_order=:sort_order, note=:note
                   WHERE id = :id""",
                data,
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"名称重复或数据冲突：{exc}") from exc
        self._replace_scripts(host_id, payload.get("scripts_parsed") or [])
        self.conn.commit()

    def delete_host(self, host_id: int) -> None:
        self.conn.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        self.conn.commit()

    def _normalize_host(self, payload: dict) -> dict:
        name = (payload.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ValidationError("名称只能用字母数字和 . _ -，1–64 字符")
        address = (payload.get("address") or "").strip()
        if not address or any(c.isspace() for c in address):
            raise ValidationError("地址不能为空，且不能含空格")
        user = (payload.get("ssh_user") or "root").strip()
        if not _NAME_RE.match(user):
            raise ValidationError("用户名不合法")
        try:
            port = int(payload.get("ssh_port") or 22)
        except (TypeError, ValueError) as exc:
            raise ValidationError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise ValidationError("端口超出范围")
        group = (payload.get("group_name") or "clients").strip()
        key_name = (payload.get("key_name") or "").strip() or None
        return {
            "name": name,
            "group_name": group,
            "address": address,
            "ssh_user": user,
            "ssh_port": port,
            "key_name": key_name,
            "detach": 1 if payload.get("detach") else 0,
            "detach_log": (payload.get("detach_log") or "/var/log/netbird-upgrade.log").strip(),
            "detach_unit_pattern": (payload.get("detach_unit_pattern") or "netbird-upgrade-*").strip(),
            "self_host": 1 if payload.get("self_host") else 0,
            "sort_order": int(payload.get("sort_order") or 0),
            "note": (payload.get("note") or "").strip(),
            "created_at": payload.get("created_at") or now_iso(),
        }

    def _replace_scripts(self, host_id: int, scripts: list[dict]) -> None:
        self.conn.execute("DELETE FROM scripts WHERE host_id = ?", (host_id,))
        for pos, s in enumerate(scripts):
            self.conn.execute(
                """INSERT INTO scripts (host_id, position, path, label, options, default_all, timeout)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    host_id,
                    pos,
                    s["path"],
                    s.get("label", ""),
                    ",".join(s.get("options") or []),
                    int(s.get("default_all", 1)),
                    s.get("timeout"),
                ),
            )

    # ---------- 任务 ----------

    def save_job(self, job_id: str, status: str, spec: dict, result: dict | None) -> None:
        self.conn.execute(
            """INSERT INTO jobs (id, created_at, finished_at, status, spec, result)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 finished_at=excluded.finished_at, status=excluded.status, result=excluded.result""",
            (
                job_id,
                spec.get("created_at") or now_iso(),
                spec.get("finished_at"),
                status,
                json.dumps(spec, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
            ),
        )
        self.conn.commit()

    def list_jobs(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, created_at, finished_at, status, spec, result FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "finished_at": r["finished_at"],
                    "status": r["status"],
                    "spec": json.loads(r["spec"]),
                    "result": json.loads(r["result"]) if r["result"] else None,
                }
            )
        return out

    def get_job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "spec": json.loads(row["spec"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def prune_jobs(self, keep: int) -> None:
        rows = self.conn.execute(
            "SELECT id FROM jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?", (keep,)
        ).fetchall()
        for r in rows:
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (r["id"],))
        self.conn.commit()
