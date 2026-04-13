# scripts/_files.py
"""文件 I/O：inbox_unread.jsonl / inbox_read.jsonl / world_state.json / daemon.log。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _data_dir(workspace: Path, ensure: bool = False) -> Path:
    """返回 {workspace}/clawsocial/ 数据目录。ensure=True 时自动创建。"""
    d = workspace / "clawsocial"
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def append_unread(workspace: Path, event: dict) -> None:
    """追加一条 JSON 事件到未读文件（同步，线程安全）"""
    path = _data_dir(workspace, ensure=True) / "inbox_unread.jsonl"
    line = json.dumps(event, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_read(workspace: Path, event: dict) -> None:
    """追加一条已读事件（最多 200 条）"""
    path = _data_dir(workspace, ensure=True) / "inbox_read.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    # 超过 200 条时截断
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > 200:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-200:])


def read_unread_events(workspace: Path) -> list[dict]:
    """读取所有未读事件"""
    path = _data_dir(workspace, ensure=True) / "inbox_unread.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def ack_events(workspace: Path, ids: list[str]) -> None:
    """将指定 ID 的未读事件移到已读"""
    ids_set = set(str(i) for i in ids)
    remaining = []
    for ev in read_unread_events(workspace):
        ev_id = str(ev.get("id", ""))
        if ev_id in ids_set:
            append_read(workspace, ev)
        else:
            remaining.append(json.dumps(ev, ensure_ascii=False) + "\n")
    path = _data_dir(workspace, ensure=True) / "inbox_unread.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for line in remaining:
            f.write(line)


def write_world_state(workspace: Path, state: dict) -> None:
    """覆盖写 world_state.json"""
    path = _data_dir(workspace, ensure=True) / "world_state.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def read_world_state(workspace: Path) -> dict:
    """读取 world_state.json"""
    path = _data_dir(workspace) / "world_state.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def write_daemon_log(workspace: Path, level: str, msg: str) -> None:
    """追加一条日志到 daemon.log"""
    path = _data_dir(workspace, ensure=True) / "daemon.log"
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {level.upper()}  {msg}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def write_pid(workspace: Path, pid: int) -> None:
    """写入 daemon.pid"""
    path = _data_dir(workspace, ensure=True) / "daemon.pid"
    path.write_text(str(pid), encoding="utf-8")


def read_pid(workspace: Path) -> int | None:
    """读取 daemon.pid"""
    path = _data_dir(workspace) / "daemon.pid"
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def remove_pid(workspace: Path) -> None:
    """删除 daemon.pid"""
    path = _data_dir(workspace) / "daemon.pid"
    path.unlink(missing_ok=True)
