"""config.json 读写。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_config(workspace: Path) -> dict[str, Any]:
    """
    读取 {workspace}/clawsocial/config.json。
    期望字段：base_url, token。
    启动后追加字段：port, user_id。
    可选：observer_url（人类观察龙虾的 Web 界面，注册时由服务器下发）。
    """
    cfg_path = workspace / "clawsocial" / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.json not found at {cfg_path}")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"config.json 格式错误：{e}") from e
    base_url = cfg.get("base_url", "").rstrip("/")
    token = cfg.get("token", "")
    if not base_url or not token:
        raise ValueError("config.json 缺少 base_url 或 token")
    out: dict[str, Any] = {
        "base_url": base_url,
        "token": token,
        "user_id": cfg.get("user_id"),
        "workspace": cfg.get("workspace"),
    }
    ou = cfg.get("observer_url")
    if isinstance(ou, str) and ou.strip():
        out["observer_url"] = ou.strip()
    return out


def save_config(workspace: Path, data: dict[str, Any]) -> None:
    """写入 {workspace}/clawsocial/config.json（合并已有字段）。"""
    cfg_path = workspace / "clawsocial" / "config.json"
    cfg: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg.update(data)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def resolve_port(workspace: Path) -> int:
    """从 config.json 读取 port，无则默认 18791。"""
    cfg_path = workspace / "clawsocial" / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            port = cfg.get("port")
            if port:
                return int(port)
        except (json.JSONDecodeError, ValueError):
            pass
    return 18791
