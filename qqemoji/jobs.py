"""扫描 / 导出的后台任务实现。

界面（Tkinter）与命令行共用这里的逻辑：调用方只需要提交一个
:class:`qqemoji.tasks.Task`，然后在自己的事件循环里查询 ``task.to_dict()``。
"""

from __future__ import annotations

from pathlib import Path

from . import config, exporter, scanner, store, tasks


def scan(task: tasks.Task, accounts: list[str], sources: list[str] | None = None) -> dict:
    """扫描若干账号目录并写入索引。"""
    account_dirs = [Path(p) for p in accounts]
    if not account_dirs:
        raise ValueError("没有选择任何 QQ 账号目录")

    index = store.get_index()
    summary: dict[str, dict] = {}
    grand_total = 0

    for acc_index, account_path in enumerate(account_dirs, 1):
        uin = account_path.name
        task.update(
            stage=f"扫描账号 {uin}",
            message=f"（{acc_index}/{len(account_dirs)}）",
            current=0,
        )
        records: list[dict] = []
        stats: dict[str, int] = {}

        def progress(stage: str, current: int = 0, message: str = "", total: int | None = None,
                     _uin: str = uin, _records: list = records) -> None:
            task.update(stage=f"[{_uin}] {stage}", current=len(_records), message=message)

        for record in scanner.scan_account(account_path, uin, sources, progress):
            if task.cancelled:
                raise RuntimeError("任务已取消")
            records.append(record)
            stats[record["source"]] = stats.get(record["source"], 0) + 1
            if len(records) % 200 == 0:
                task.update(current=len(records), total=0,
                            message=f"已发现 {len(records)} 个表情")

        task.update(stage=f"[{uin}] 写入索引", current=len(records),
                    message=f"{len(records)} 个表情")
        index.clear_account(str(account_path))
        batch: list[dict] = []
        for record in records:
            if task.cancelled:
                raise RuntimeError("任务已取消")
            batch.append(record)
            if len(batch) >= 500:
                index.upsert_many(scanner.enrich_batch(batch))
                batch.clear()
        if batch:
            index.upsert_many(scanner.enrich_batch(batch))

        grand_total += len(records)
        summary[uin] = {"total": len(records), "by_source": stats, "path": str(account_path)}

    import time

    index.set_meta("scanned_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    index.set_meta("last_scan_report", str(summary))

    return {
        "total": grand_total,
        "accounts": summary,
        "sources": sources or list(config.SOURCES),
        "stats": index.stats(),
    }


def export(task: tasks.Task, payload: dict) -> dict:
    """导出表情：``payload`` 与前端表单字段一致。"""
    index = store.get_index()

    if payload.get("ids"):
        records = index.get_many(payload["ids"])
    else:
        records = index.all_matching(payload.get("filters") or {})

    def progress(stage: str, current: int, total: int, message: str = "") -> None:
        task.update(stage=stage, current=current, total=total, message=message)

    result = exporter.export_items(
        records,
        mode=payload.get("mode", "zip"),
        target_dir=payload.get("target_dir"),
        convert=payload.get("convert", "origin"),
        max_side=int(payload.get("max_side") or 0),
        dedupe=bool(payload.get("dedupe", True)),
        name_template=payload.get("name_template") or "{index}_{pack}_{name}",
        progress=progress,
        should_cancel=lambda: task.cancelled,
    )
    exporter.cleanup_exports()
    return result
