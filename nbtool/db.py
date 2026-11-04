"""SQLite 持久化：机器、脚本、任务、设置。

机器和脚本在界面上是一起编辑的（脚本用多行文本录入），所以 scripts 单独成表、
由 hosts 级联删除；任务只存摘要，逐行日志落在 data/jobs/<id>.log。
"""

from __future__ import annotations

import json
import re
import shlex
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
    priority            INTEGER NOT NULL DEFAULT 10,
    sudo                INTEGER NOT NULL DEFAULT 0,
    last_check          TEXT    NOT NULL DEFAULT '',
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
    args        TEXT    NOT NULL DEFAULT '[]',
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

# 机器名只是标签，允许中文；不能有空格、斜杠、控制字符
_BAD_NAME_CHARS = set("/\\:*?\"<>| \t\r\n\x00")
_OPTION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _loads(raw, default=None):
    if raw is None or raw == "":
        return {} if default is None else default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {} if default is None else default


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ValidationError(ValueError):
    """用户输入不合法，返回 400。"""


def parse_scripts(text: str) -> list[dict]:
    """把「命令 | 显示名 | 参数」的多行文本解析成脚本列表。

    顺序即执行顺序；空行和 # 开头的行忽略。参数用 shlex 解析，支持引号。
    """
    out: list[dict] = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        path = parts[0].strip()
        if not path.startswith("/"):
            raise ValidationError(f"第 {lineno} 行：脚本路径必须是绝对路径（{path or '空'}）")
        if any(c in path for c in "\n\r\t"):
            raise ValidationError(f"第 {lineno} 行：路径里不能有控制字符")
        label = parts[1].strip() if len(parts) > 1 and parts[1].strip() else path.rsplit("/", 1)[-1]
        args: list[str] = []
        if len(parts) > 2 and parts[2].strip():
            try:
                args = shlex.split(parts[2])
            except ValueError as exc:
                raise ValidationError(f"第 {lineno} 行：参数解析失败（{exc}）") from exc
            for arg in args:
                if any(c in arg for c in "\n\r\t\x00"):
                    raise ValidationError(f"第 {lineno} 行：参数里不能有控制字符")
        out.append({"path": path, "label": label, "args": args, "timeout": None})
    if not out:
        raise ValidationError("至少需要一个脚本")
    return out


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给已有的库补列：CREATE TABLE IF NOT EXISTS 不会改动老表结构。"""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(hosts)")}
        if "sudo" not in columns:
            self.conn.execute("ALTER TABLE hosts ADD COLUMN sudo INTEGER NOT NULL DEFAULT 0")
        if "last_check" not in columns:
            self.conn.execute("ALTER TABLE hosts ADD COLUMN last_check TEXT NOT NULL DEFAULT ''")
        if "priority" not in columns:
            self.conn.execute("ALTER TABLE hosts ADD COLUMN priority INTEGER NOT NULL DEFAULT 10")
            # 原来的 console 分组换成优先级 1，其余保持 10
            self.conn.execute("UPDATE hosts SET priority = 1 WHERE group_name = 'console'")

        script_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(scripts)")}
        if "args" not in script_columns:
            self.conn.execute("ALTER TABLE scripts ADD COLUMN args TEXT NOT NULL DEFAULT '[]'")
            if "options" in script_columns:
                # 老的逗号分隔组件名 → 参数数组
                for row in self.conn.execute("SELECT id, options FROM scripts").fetchall():
                    args = [a.strip() for a in (row[1] or "").split(",") if a.strip()]
                    self.conn.execute(
                        "UPDATE scripts SET args = ? WHERE id = ?", (json.dumps(args), row[0])
                    )

    # ---------- 机器 ----------

    def list_hosts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM hosts ORDER BY self_host, priority, sort_order, id"
        ).fetchall()
        hosts = [dict(r) for r in rows]
        scripts = self._scripts_by_host()
        for h in hosts:
            h["scripts"] = scripts.get(h["id"], [])
            h["detach"] = bool(h["detach"])
            h["self_host"] = bool(h["self_host"])
            h["sudo"] = bool(h["sudo"])
            h["check"] = _loads(h.get("last_check"))
        return hosts

    def _scripts_by_host(self) -> dict[int, list[dict]]:
        rows = self.conn.execute(
            "SELECT * FROM scripts ORDER BY host_id, position, id"
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for r in rows:
            s = dict(r)
            s["args"] = _loads(s.get("args"), default=[])
            s.pop("options", None)
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
        host["sudo"] = bool(host["sudo"])
        host["check"] = _loads(host.get("last_check"))
        return host

    def create_host(self, payload: dict) -> int:
        data = self._normalize_host(payload)
        cur = self.conn.execute(
            """INSERT INTO hosts (name, priority, address, ssh_user, ssh_port, key_name,
                                  detach, detach_log, detach_unit_pattern, self_host, sudo,
                                  sort_order, note, created_at)
               VALUES (:name, :priority, :address, :ssh_user, :ssh_port, :key_name,
                       :detach, :detach_log, :detach_unit_pattern, :self_host, :sudo,
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
                """UPDATE hosts SET name=:name, priority=:priority, address=:address,
                          ssh_user=:ssh_user, ssh_port=:ssh_port, key_name=:key_name,
                          detach=:detach, detach_log=:detach_log,
                          detach_unit_pattern=:detach_unit_pattern, self_host=:self_host,
                          sudo=:sudo, sort_order=:sort_order, note=:note
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

    def _normalize_host(self, payload: dict) -> dict:  # noqa: C901
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationError("名称不能为空")
        if len(name) > 64:
            raise ValidationError("名称最多 64 个字符")
        bad = sorted(set(name) & _BAD_NAME_CHARS)
        if bad:
            raise ValidationError(
                "名称里不能包含 " + "、".join(repr(c) for c in bad) + "（不能有空格、斜杠、冒号等）"
            )
        address = (payload.get("address") or "").strip()
        if not address or any(c.isspace() for c in address):
            raise ValidationError("地址不能为空，且不能含空格")
        user = (payload.get("ssh_user") or "root").strip()
        if not re.match(r"^[A-Za-z0-9._-]{1,32}$", user):
            raise ValidationError("用户名只能用字母、数字和 . _ -")
        try:
            port = int(payload.get("ssh_port") or 22)
        except (TypeError, ValueError) as exc:
            raise ValidationError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise ValidationError("端口超出范围")
        key_name = (payload.get("key_name") or "").strip() or None
        try:
            priority = int(payload.get("priority") if payload.get("priority") is not None else 10)
        except (TypeError, ValueError):
            raise ValidationError("优先级必须是数字")
        if not -999 <= priority <= 999:
            raise ValidationError("优先级超出范围（-999 ~ 999）")
        return {
            "name": name,
            "priority": priority,
            "address": address,
            "ssh_user": user,
            "ssh_port": port,
            "key_name": key_name,
            "detach": 1 if payload.get("detach") else 0,
            "detach_log": (payload.get("detach_log") or "/var/log/netbird-upgrade.log").strip(),
            "detach_unit_pattern": (payload.get("detach_unit_pattern") or "netbird-upgrade-*").strip(),
            "self_host": 1 if payload.get("self_host") else 0,
            "sudo": 1 if payload.get("sudo") else 0,
            "sort_order": int(payload.get("sort_order") or 0),
            "note": (payload.get("note") or "").strip(),
            "created_at": payload.get("created_at") or now_iso(),
        }

    def _replace_scripts(self, host_id: int, scripts: list[dict]) -> None:
        self.conn.execute("DELETE FROM scripts WHERE host_id = ?", (host_id,))
        for pos, s in enumerate(scripts):
            self.conn.execute(
                """INSERT INTO scripts (host_id, position, path, label, args, timeout)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    host_id,
                    pos,
                    s["path"],
                    s.get("label", ""),
                    json.dumps(s.get("args") or [], ensure_ascii=False),
                    s.get("timeout"),
                ),
            )

    def set_check(self, host_id: int, check: dict) -> None:
        """把最近一次体检结果落库，重启后界面不会全变回「未检查」。"""
        self.conn.execute(
            "UPDATE hosts SET last_check = ? WHERE id = ?",
            (json.dumps(check, ensure_ascii=False), host_id),
        )
        self.conn.commit()

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
