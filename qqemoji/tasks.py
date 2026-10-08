"""后台任务管理（扫描 / 导出），带进度查询与取消。"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Task:
    id: str
    kind: str
    title: str
    state: str = "pending"  # pending / running / done / error / cancelled
    stage: str = ""
    message: str = ""
    current: int = 0
    total: int = 0
    result: dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def update(self, stage: str | None = None, current: int | None = None,
               total: int | None = None, message: str | None = None) -> None:
        if stage is not None:
            self.stage = stage
        if current is not None:
            self.current = current
        if total is not None:
            self.total = total
        if message is not None:
            self.message = message

    def to_dict(self) -> dict:
        elapsed = (self.finished or time.time()) - (self.started or self.created)
        percent = 0.0
        if self.total > 0:
            percent = min(100.0, self.current * 100.0 / self.total)
        elif self.state == "done":
            percent = 100.0
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "stage": self.stage,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "percent": round(percent, 1),
            "result": self.result,
            "error": self.error,
            "elapsed": round(elapsed, 2),
            "created": self.created,
        }


class TaskManager:
    def __init__(self, workers: int = 3) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qqemoji")
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._latest: dict[str, str] = {}

    def submit(self, kind: str, title: str, fn: Callable[[Task], dict],
               dedupe_key: str | None = None, replace: bool = False) -> Task:
        """提交任务。

        :param dedupe_key: 同 key 的旧任务会被取消（用于「重新扫描」等场景）
        :param replace: 若同 key 任务仍在运行，直接复用而不新建
        """
        if dedupe_key:
            with self._lock:
                existing_id = self._latest.get(dedupe_key)
                existing = self._tasks.get(existing_id) if existing_id else None
            if existing and existing.state in ("pending", "running"):
                if replace:
                    existing.cancel()
                else:
                    return existing

        task = Task(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._tasks[task.id] = task
            if dedupe_key:
                self._latest[dedupe_key] = task.id
            if len(self._tasks) > 60:
                for old_id in sorted(self._tasks, key=lambda i: self._tasks[i].created)[:20]:
                    if self._tasks[old_id].state in ("done", "error", "cancelled"):
                        self._tasks.pop(old_id, None)

        def runner() -> None:
            task.state = "running"
            task.started = time.time()
            try:
                result = fn(task)
                if task.cancelled:
                    task.state = "cancelled"
                else:
                    task.state = "done"
                    task.result = result or {}
            except Exception as exc:  # noqa: BLE001 - 需要把异常回传给前端
                task.state = "error"
                task.error = f"{type(exc).__name__}: {exc}"
                task.result = {"traceback": traceback.format_exc()[-4000:]}
            finally:
                task.finished = time.time()

        self._pool.submit(runner)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task and task.state in ("pending", "running"):
            task.cancel()
            return True
        return False

    def latest(self, key: str) -> Task | None:
        with self._lock:
            task_id = self._latest.get(key)
        return self.get(task_id) if task_id else None

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)[:limit]
        return [t.to_dict() for t in tasks]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


manager = TaskManager()


def progress_bridge(task: Task) -> Callable[..., None]:
    """把 (stage, current, message) 形式的回调转成任务进度。"""

    def cb(stage: str, current: int = 0, message: str = "", total: int | None = None) -> None:
        task.update(stage=stage, current=current, message=message, total=total)

    return cb
