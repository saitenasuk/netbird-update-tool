"""任务注册表：行缓冲、SSE 等待、日志落盘。

设计要点：日志是服务端状态，浏览器断线不影响任务；重新连上时用 ?from=N 补齐。
所以行缓冲必须完整保留到任务结束（保留期由 keep_jobs 的日志文件兜底）。
"""

from __future__ import annotations

import json
import random
import string
import threading
import time
from datetime import datetime
from pathlib import Path


def new_job_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{stamp}-{suffix}"


class Job:
    def __init__(self, job_id: str, spec: dict, log_path: Path):
        self.id = job_id
        self.spec = spec
        self.status = "running"
        self.result: dict | None = None
        self.created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.finished_at: str | None = None
        self._lines: list[dict] = []
        self._cond = threading.Condition()
        self._seq = 0
        self._abort = threading.Event()
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)

    # ---------- 写入 ----------

    def emit(self, host: str, text: str, kind: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._cond:
            self._seq += 1
            entry = {"seq": self._seq, "t": stamp, "host": host, "kind": kind, "text": text}
            self._lines.append(entry)
            self._cond.notify_all()
        prefix = f"[{stamp}] " + (f"[{host}] " if host else "")
        try:
            self._fh.write(prefix + text + "\n")
        except Exception:
            pass

    def finish(self, status: str, result: dict | None) -> None:
        with self._cond:
            self.status = status
            self.result = result
            self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._cond.notify_all()
        try:
            self._fh.close()
        except Exception:
            pass

    def abort(self) -> None:
        self._abort.set()
        self.emit("", "收到中止请求", "err")

    def is_aborted(self) -> bool:
        return self._abort.is_set()

    # ---------- 读取 ----------

    def snapshot(self, since: int = 0) -> tuple[list[dict], bool]:
        with self._cond:
            lines = [l for l in self._lines if l["seq"] > since]
            return lines, self.status != "running"

    def poll(self, since: int, timeout: float = 20.0) -> tuple[list[dict], bool]:
        """返回 since 之后的新行；没有新行时最多等 timeout 秒。done 表示任务已结束。"""
        with self._cond:
            if self.status == "running" and not any(l["seq"] > since for l in self._lines):
                self._cond.wait(timeout=timeout)
            new = [l for l in self._lines if l["seq"] > since]
            done = self.status != "running"
        return new, done

    def to_dict(self) -> dict:
        with self._cond:
            return {
                "id": self.id,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "status": self.status,
                "spec": self.spec,
                "result": self.result,
                "lines": len(self._lines),
                "last_seq": self._seq,
            }


class JobManager:
    def __init__(self, store, log_dir: Path, keep: int = 50):
        self.store = store
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._active: Job | None = None

    def create(self, spec: dict) -> Job:
        with self._lock:
            if self._active is not None and self._active.status == "running":
                raise RuntimeError("已有任务在运行")
            job_id = new_job_id()
            job = Job(job_id, spec, self.log_dir / f"{job_id}.log")
            self._jobs[job.id] = job
            self._active = job
        spec = dict(spec, created_at=job.created_at)
        self.store.save_job(job.id, "running", spec, None)
        return job

    def start(self, job: Job, worker) -> threading.Thread:
        def runner() -> None:
            try:
                result = worker()
                job.finish(result.get("status", "success"), result)
            except Exception as exc:  # 任务线程不能静默死掉
                job.emit("", f"任务异常：{exc}", "err")
                job.finish("failed", {"status": "failed", "hosts": [], "error": str(exc)})
            finally:
                self.store.save_job(job.id, job.status, job.spec, job.result)
                self.store.prune_jobs(self.keep)

        thread = threading.Thread(target=runner, daemon=True, name=f"job-{job.id}")
        thread.start()
        return thread

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Job | None:
        with self._lock:
            job = self._active
            return job if job and job.status == "running" else None

    def read_log(self, job_id: str) -> str:
        path = self.log_dir / f"{job_id}.log"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")


def summarize(job: Job) -> str:
    return json.dumps(job.to_dict(), ensure_ascii=False)
