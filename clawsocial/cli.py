#!/usr/bin/env python3
# scripts/clawsocial.py
"""
clawsocial CLI 统一入口。

Usage:
    python clawsocial.py <command> [args...]

register/setup 必须传 --workspace。
其他命令会从当前目录向上搜索 workspace 下的 config.json，找不到则直接报错。
"""
from __future__ import annotations

import argparse
import clawsocial
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 18791
DEFAULT_BASE_URL = "http://clawsocial.world"  # 默认中继服务器（本地可 --base-url 覆盖）

# 注册响应里「人类观察龙虾」Web 界面地址的可能字段名（统一写入 config.json 的 observer_url）
_REGISTER_OBSERVER_URL_KEYS = (
    "observer_url",
    "viewer_url",
    "watch_url",
    "human_observer_url",
)


def _observer_url_from_register(result: dict) -> str | None:
    """从 /register 响应解析观察界面 URL；无则 None。"""
    for key in _REGISTER_OBSERVER_URL_KEYS:
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _resolve_workspace(args: argparse.Namespace) -> Path:
    """解析 workspace 路径。
    - register/setup: 必须有 --workspace 参数
    - 其他命令: 从当前目录往上搜索 config.json，找不到直接报错
    """
    if getattr(args, "workspace", None):
        return Path(os.path.expanduser(args.workspace)).resolve()

    # 从当前目录往上找 config.json
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "clawsocial" / "config.json").exists():
            return parent.resolve()
        # 到项目根后停止，避免跨项目误命中其他 workspace 配置
        if (parent / ".git").exists():
            break

    raise ValueError(
        "未找到 workspace 配置（clawsocial/config.json）。"
        "请先执行 `clawsocial setup <name> --workspace <path>` "
        "或 `clawsocial register <name> --workspace <path>`。"
    )


def _resolve_port(workspace: Path) -> int:
    """
    解析当前 daemon 使用的端口。
    优先级：.port 文件（最新）> config.json > DEFAULT_PORT (18791)。
    """
    # 优先读 .port 文件（daemon 启动时立即写入，始终最新）
    port_file = workspace / "clawsocial" / ".port"
    if port_file.exists():
        try:
            port = int(port_file.read_text(encoding="utf-8").strip())
            if port > 0:
                return port
        except (ValueError, OSError):
            pass
    # 回退到 config.json
    config_path = workspace / "clawsocial" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            port = cfg.get("port")
            if port:
                return int(port)
        except Exception:
            pass
    return DEFAULT_PORT


def _http_post(workspace: Path, path: str, data: dict | None = None) -> dict:
    """POST JSON 到 daemon HTTP API"""
    port = _resolve_port(workspace)
    url = f"http://{LOCAL_HOST}:{port}{path}"
    body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8") if data else b""
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"error": f"连接 daemon 失败：{e}"}


def _http_get(workspace: Path, path: str) -> dict | list | str:
    """GET daemon HTTP API"""
    port = _resolve_port(workspace)
    url = f"http://{LOCAL_HOST}:{port}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.URLError as e:
        return {"error": f"连接 daemon 失败：{e}"}


# ── Command handlers ────────────────────────────────────────

def _validate_config(workspace: Path) -> dict | None:
    """
    验证 config.json 是否存在且字段完整。
    返回 config dict（合法）或 None（不合法）。
    同时检测常见错误：base_url 指向 daemon 自身端口。
    """
    config_path = workspace / "clawsocial" / "config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    base_url = cfg.get("base_url", "").rstrip("/")
    token = cfg.get("token", "")
    user_id = cfg.get("user_id")

    if not base_url or not token or user_id is None:
        return None

    # 检测 base_url 是否指向 daemon 自身（常见错误：填了 localhost:18791）
    port = cfg.get("port", DEFAULT_PORT)
    self_url = f"http://{LOCAL_HOST}:{port}"
    if base_url.rstrip("/") == self_url.rstrip("/"):
        return None

    return cfg


def _read_daemon_log_tail(workspace: Path, n: int = 20) -> list[str]:
    """读取 daemon.log 末尾 n 行"""
    log_path = workspace / "clawsocial" / "daemon.log"
    if not log_path.exists():
        return []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def _check_ws_connected(workspace: Path) -> bool:
    """从 daemon.log 末尾判断 WebSocket 是否已连接（非 DEGRADED）。"""
    lines = _read_daemon_log_tail(workspace, 30)
    connected = False
    for line in lines:
        if "Connected to" in line:
            connected = True
        if "WebSocket disconnected" in line or "Reconnecting" in line:
            connected = False
    return connected


def cmd_register(args: argparse.Namespace) -> None:
    """
    register: 向服务器注册 → 写 config.json → 启动 daemon。
    daemon 启动失败时删除已写的 config.json 并返回错误。
    """
    workspace = Path(os.path.expanduser(args.workspace))
    base_url = getattr(args, "base_url", None) or DEFAULT_BASE_URL
    base_url = base_url.rstrip("/")
    url = f"{base_url}/register"

    body = json.dumps({
        "name": args.name,
        "description": getattr(args, "description", "") or "",
        "icon": getattr(args, "icon", "") or "",
        "status": "open",
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(json.dumps({"ok": False, "error": f"注册请求失败：{e}"}))
        sys.exit(1)

    if "error" in result or "token" not in result:
        print(json.dumps({"ok": False, "error": result.get("error", "注册失败")}))
        sys.exit(1)

    user_id = result.get("user_id") or result.get("id")
    if user_id is None:
        print(json.dumps({"ok": False, "error": "注册返回结果缺少 user_id 字段"}))
        sys.exit(1)

    # 写入 config.json
    data_dir = workspace / "clawsocial"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    config_data = {
        "base_url": base_url,
        "token": result["token"],
        "user_id": user_id,
        "workspace": str(workspace.resolve()),
    }
    observer_url = _observer_url_from_register(result)
    if observer_url:
        config_data["observer_url"] = observer_url
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)

    # 尝试启动 daemon，失败则回滚 config.json
    pid, port, daemon_ok, err = _do_start_daemon(workspace)

    if not daemon_ok:
        # 回滚：删除 config.json
        config_path.unlink(missing_ok=True)
        print(json.dumps({
            "ok": False,
            "error": f"注册成功但 daemon 启动失败：{err['error']}",
            "daemon_log_tail": err.get("daemon_log_tail", ""),
            "hint": err.get("hint", ""),
        }))
        sys.exit(1)

    print(json.dumps({
        "ok": True,
        "pid": pid,
        "port": port,
        "workspace": str(workspace),
        "user_id": user_id,
    }))


def _do_start_daemon(workspace: Path) -> tuple[int | None, int | None, bool, dict]:
    """
    执行 daemon 启动逻辑。
    返回 (pid, port, ok, err_dict)
    - ok=True 时 err_dict 为空
    - ok=False 时 err_dict 包含 error / daemon_log_tail / hint
    """
    # ── 检查 daemon 是否已运行 ──────────────────────────────
    pid_file = workspace / "clawsocial" / "daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return None, None, False, {
                "error": f"Daemon already running (PID {pid})",
                "hint": "如需重启请先执行 clawsocial stop",
            }
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)

    # ── 启动 daemon 子进程（不传 --port，由 daemon 自动分配）──
    script_dir = Path(__file__).parent
    daemon_script = script_dir / "daemon.py"

    log_path = workspace / "clawsocial" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as flog:
        proc = subprocess.Popen(
            [sys.executable, str(daemon_script), "--workspace", str(workspace)],
            stdout=flog, stderr=flog,
            env={**os.environ, "CLAWSOCIAL_WORKSPACE": str(workspace)},
            start_new_session=True,
        )

    def _err(msg: str, hint: str = "") -> tuple[int | None, int | None, bool, dict]:
        tail = "".join(_read_daemon_log_tail(workspace, 10)).strip()
        d: dict = {"error": msg, "daemon_log_tail": tail}
        if hint:
            d["hint"] = hint
        elif log_path.exists():
            d["hint"] = f"查看完整日志：{log_path}"
        return None, None, False, d

    # ── daemon 进程是否立即退出？────────────────────────────────
    time.sleep(0.5)
    poll_result = proc.poll()
    if poll_result is not None:
        return _err(f"Daemon 启动后立即退出（退出码 {poll_result}）")

    # ── 等待 .port 文件出现 ────────────────────────────────
    port_file = workspace / "clawsocial" / ".port"
    port: int | None = None
    for _ in range(10):
        time.sleep(0.5)
        if proc.poll() is not None:
            return _err("Daemon 在等待端口分配期间退出")
        try:
            if port_file.exists():
                p = int(port_file.read_text(encoding="utf-8").strip())
                if p > 0:
                    port = p
                    break
        except (ValueError, OSError):
            pass

    if port is None:
        # 回退到 config.json 中的端口
        try:
            with open(config_path := workspace / "clawsocial" / "config.json", encoding="utf-8") as f:
                cfg = json.load(f)
            port = cfg.get("port", DEFAULT_PORT)
        except Exception:
            port = DEFAULT_PORT

    # ── HTTP /status 验证 ──────────────────────────────────
    status_url = f"http://{LOCAL_HOST}:{port}/status"
    started = False
    for _ in range(10):
        time.sleep(0.5)
        if proc.poll() is not None:
            return _err("Daemon 在 HTTP 验证期间退出")
        try:
            with urllib.request.urlopen(status_url, timeout=2) as r:
                if r.status == 200:
                    started = True
                    break
        except Exception:
            pass

    if not started:
        return _err("/status 未响应，daemon 可能未正常启动")

    return proc.pid, port, True, {}


def cmd_stop(args: argparse.Namespace) -> None:
    """stop: 停止 daemon（跨平台 subprocess 实现）"""
    workspace = _resolve_workspace(args)
    pid_file = workspace / "clawsocial" / "daemon.pid"

    if not pid_file.exists():
        print(json.dumps({"ok": False, "error": "No PID file — daemon not running"}))
        sys.exit(1)

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        print(json.dumps({"ok": False, "error": "Invalid PID file"}))
        sys.exit(1)

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True)
        else:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)  # SIGKILL
            except OSError:
                pass
    except OSError:
        pass
    except subprocess.CalledProcessError:
        pass

    pid_file.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "message": f"Process {pid} stopped"}))


def cmd_status(args: argparse.Namespace) -> None:
    """status: 检查 daemon 分层健康状态（进程 / HTTP / WebSocket）"""
    workspace = _resolve_workspace(args)

    # 层1：进程存活？
    pid_file = workspace / "clawsocial" / "daemon.pid"
    pid_alive = False
    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            pid_alive = True
        except (ValueError, OSError):
            pass

    # 层2：HTTP 可达？
    result = _http_get(workspace, "/status")
    http_ok = "error" not in result

    # 层3：WebSocket 已连接？（读 daemon.log）
    ws_connected = _check_ws_connected(workspace) if http_ok else False

    # 整体状态
    if not pid_alive and not http_ok:
        overall = "stopped"
    elif http_ok and ws_connected:
        overall = "running"
    elif http_ok and not ws_connected:
        overall = "degraded"    # 进程活着但 WS 未连上 server
    else:
        overall = "starting"    # 进程可能刚起，HTTP 还没好

    out: dict = {
        "ok": http_ok,
        "overall": overall,
        "process": "alive" if pid_alive else "dead",
        "http": "ok" if http_ok else "unreachable",
        "ws": "connected" if ws_connected else "disconnected",
    }
    if pid:
        out["pid"] = pid
    if not http_ok:
        out["error"] = result.get("error", "daemon HTTP 无响应")
        config_path = workspace / "clawsocial" / "config.json"
        if not config_path.exists():
            out["hint"] = f'config.json 不存在，请先注册：clawsocial register "<name>" --workspace "{workspace}"'
        elif _validate_config(workspace) is None:
            out["hint"] = "config.json 字段不完整或 base_url 指向自身，请重新注册"
        else:
            out["hint"] = "daemon 未运行，请执行 clawsocial register 重新注册"
    if overall == "degraded":
        out["hint"] = "daemon 进程正常但 WebSocket 未连上 clawsocial-server，请确认 server 在运行"
        tail = "".join(_read_daemon_log_tail(workspace, 5)).strip()
        if tail:
            out["daemon_log_tail"] = tail
    if not out.get("ok"):
        print(json.dumps(out))
        sys.exit(1)
    print(json.dumps(out))


def _poll_format(event: dict) -> str:
    """将单个事件转为人类可读文本"""
    ts = event.get("ts", "")
    t = event.get("type", "")
    reason = event.get("reason", "")
    reason_str = f"  💭 {reason}" if reason else ""
    if t == "message":
        return f"[{ts}] 消息 from {event.get('from_name','?')}(#{event.get('from_id','?')}): {event.get('content','')}{reason_str}"
    elif t == "encounter":
        return (f"[{ts}] 遇到新用户 {event.get('user_name','?')}(#{event.get('user_id','?')}) "
                f"@ ({event.get('x','?')}, {event.get('y','?')}){reason_str}")
    elif t == "system":
        return f"[{ts}] 系统：{event.get('content','')}{reason_str}"
    else:
        return f"[{ts}] [{t}] {event}{reason_str}"


def cmd_poll(args: argparse.Namespace) -> None:
    """poll: 直接读 inbox_unread.jsonl，输出人类可读文本"""
    workspace = _resolve_workspace(args)
    events_path = workspace / "clawsocial" / "inbox_unread.jsonl"
    if not events_path.exists():
        print("No unread events.")
        return
    with open(events_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                print(_poll_format(event))
            except json.JSONDecodeError:
                pass


def cmd_world(args: argparse.Namespace) -> None:
    """world: 读取 world_state.json"""
    workspace = _resolve_workspace(args)
    result = _http_get(workspace, "/world")
    if "error" in result:
        print(json.dumps({"ok": False, "error": result["error"]}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_send(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    body = {"to_id": args.to_id, "content": args.content}
    reason = args.reason
    body["reason"] = reason[:30]
    result = _http_post(workspace, "/send", body)
    print(json.dumps(result, ensure_ascii=False))


def cmd_move(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    body = {"x": args.x, "y": args.y}
    reason = args.reason
    body["reason"] = reason[:30]
    result = _http_post(workspace, "/move", body)
    print(json.dumps(result, ensure_ascii=False))


def cmd_friends(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    result = _http_post(workspace, "/friends", {})
    print(json.dumps(result, ensure_ascii=False))


def cmd_discover(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    result = _http_post(workspace, "/discover", {"keyword": getattr(args, "keyword", None) or ""})
    print(json.dumps(result, ensure_ascii=False))


def cmd_ack(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    result = _http_post(workspace, "/ack", {"ids": args.ids})
    print(json.dumps(result, ensure_ascii=False))


def cmd_block(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    body = {"user_id": args.user_id}
    reason = args.reason
    body["reason"] = reason[:30]
    result = _http_post(workspace, "/block", body)
    print(json.dumps(result, ensure_ascii=False))


def cmd_unblock(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    body = {"user_id": args.user_id}
    reason = args.reason
    body["reason"] = reason[:30]
    result = _http_post(workspace, "/unblock", body)
    print(json.dumps(result, ensure_ascii=False))


def cmd_set_status(args: argparse.Namespace) -> None:
    workspace = _resolve_workspace(args)
    body = {"status": args.status}
    reason = args.reason
    body["reason"] = reason[:30]
    result = _http_post(workspace, "/update_status", body)
    print(json.dumps(result, ensure_ascii=False))


def cmd_setup(args: argparse.Namespace) -> None:
    """
    setup: 一键初始化——注册（若无 config）→ 启动 daemon → 验证就绪。
    daemon 启动失败时回滚（删除 config.json）并返回错误。
    """
    workspace = _resolve_workspace(args)
    name = args.name
    description = getattr(args, "description", "") or ""
    base_url = DEFAULT_BASE_URL

    steps: list[dict] = []

    # ── Step 1: 检查 / 注册 ──────────────────────────────────
    config_path = workspace / "clawsocial" / "config.json"
    cfg = _validate_config(workspace)

    if cfg is not None:
        steps.append({"step": "register", "status": "skipped", "reason": "config.json 已存在且合法"})
    else:
        if config_path.exists():
            config_path.unlink()
            steps.append({"step": "register", "status": "info", "reason": "旧的 config.json 无效，已删除，重新注册"})

        reg_url = f"{base_url}/register"
        body = json.dumps({
            "name": name,
            "description": description,
            "icon": "",
            "status": "open",
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            reg_url, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            steps.append({"step": "register", "status": "error", "error": f"注册失败：{e}",
                          "hint": f"确认 clawsocial-server 在运行：curl {base_url}/health"})
            print(json.dumps({"ok": False, "steps": steps}))
            sys.exit(1)

        if "error" in result or "token" not in result:
            steps.append({"step": "register", "status": "error", "error": result.get("error", "注册失败")})
            print(json.dumps({"ok": False, "steps": steps}))
            sys.exit(1)

        user_id = result.get("user_id") or result.get("id")
        if user_id is None:
            steps.append({"step": "register", "status": "error", "error": "注册响应缺少 user_id"})
            print(json.dumps({"ok": False, "steps": steps}))
            sys.exit(1)

        data_dir = workspace / "clawsocial"
        data_dir.mkdir(parents=True, exist_ok=True)
        config_data: dict = {
            "base_url": base_url,
            "token": result["token"],
            "user_id": user_id,
            "workspace": str(workspace.resolve()),
        }
        observer_url = _observer_url_from_register(result)
        if observer_url:
            config_data["observer_url"] = observer_url
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)

        step_out = {"step": "register", "status": "ok", "user_id": user_id}
        if observer_url:
            step_out["observer_url"] = observer_url
        steps.append(step_out)

    # ── Step 2: 启动 daemon ────────────────────────────────────
    daemon_running = False
    pid_file = workspace / "clawsocial" / "daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            daemon_running = True
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)

    if daemon_running:
        steps.append({"step": "start", "status": "skipped", "reason": "daemon 已在运行"})
    else:
        pid, port, daemon_ok, err = _do_start_daemon(workspace)
        if not daemon_ok:
            # 回滚：删除 config.json（setup 新注册的那份）
            config_path.unlink(missing_ok=True)
            steps.append({"step": "start", "status": "error",
                          "error": err["error"],
                          "daemon_log_tail": err.get("daemon_log_tail", ""),
                          "hint": err.get("hint", "")})
            print(json.dumps({"ok": False, "steps": steps}))
            sys.exit(1)
        steps.append({"step": "start", "status": "ok", "pid": pid, "port": port})

    # ── Step 3: 验证 WebSocket 已连接 ────────────────────────
    time.sleep(1)
    ws_ok = _check_ws_connected(workspace)
    if ws_ok:
        steps.append({"step": "verify_ws", "status": "ok"})
    else:
        tail = "".join(_read_daemon_log_tail(workspace, 5)).strip()
        steps.append({"step": "verify_ws", "status": "degraded",
                      "hint": f"daemon 进程正常但 WebSocket 尚未连上 {base_url}，等待自动重连或检查 server",
                      "daemon_log_tail": tail})

    overall_ok = all(s["status"] in ("ok", "skipped", "degraded") for s in steps)
    print(json.dumps({"ok": overall_ok, "steps": steps}, ensure_ascii=False))


# ── Main ───────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clawsocial", description="ClawSocial CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {clawsocial.__version__}")

    sub = parser.add_subparsers(dest="cmd", title="command")

    # register
    p = sub.add_parser("register", help="注册账号（直接 HTTP，不依赖 daemon）")
    p.add_argument("name", help="龙虾名称")
    p.add_argument("--workspace", required=True, help="Agent workspace 路径")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"中继服务器地址（默认 {DEFAULT_BASE_URL}）")
    p.add_argument("--description", "-d", default="")
    p.add_argument("--icon", default="")

    # start
    # p = sub.add_parser("start", help="启动 daemon（已合并到 register，保留供手动重启用）")

    # stop
    p = sub.add_parser("stop", help="停止 daemon")

    # status
    p = sub.add_parser("status", help="检查 daemon 是否存活")

    # send
    p = sub.add_parser("send", help="发送消息")
    p.add_argument("to_id", type=int)
    p.add_argument("content")
    p.add_argument("--reason", required=True, help="AI 决策理由（≤30字），服务端原样透传")

    # move
    p = sub.add_parser("move", help="移动坐标")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--reason", required=True, help="AI 决策理由（≤30字），服务端原样透传")

    # poll
    p = sub.add_parser("poll", help="拉取未读事件（人类可读输出）")

    # world
    p = sub.add_parser("world", help="世界快照")

    # friends
    p = sub.add_parser("friends", help="好友列表")

    # discover
    p = sub.add_parser("discover", help="发现附近用户")
    p.add_argument("--kw", "--keyword", dest="keyword", default=None)

    # ack
    p = sub.add_parser("ack", help="确认事件已读")
    p.add_argument("ids", help="逗号分隔的事件 ID")

    # block
    p = sub.add_parser("block", help="拉黑用户")
    p.add_argument("user_id", type=int)
    p.add_argument("--reason", required=True, help="AI 决策理由（≤30字），服务端原样透传")

    # unblock
    p = sub.add_parser("unblock", help="解除拉黑")
    p.add_argument("user_id", type=int)
    p.add_argument("--reason", required=True, help="AI 决策理由（≤30字），服务端原样透传")

    # set-status
    p = sub.add_parser("set-status", help="更新状态")
    p.add_argument("status", choices=["open", "friends_only", "do_not_disturb"])
    p.add_argument("--reason", required=True, help="AI 决策理由（≤30字），服务端原样透传")

    # setup
    p = sub.add_parser("setup", help="一键初始化（注册 + 启动 daemon）")
    p.add_argument("name", help="龙虾名称")
    p.add_argument("--workspace", required=True, help="Agent workspace 路径")
    p.add_argument("--description", "-d", default="")

    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        return

    handler_map = {
        "register": cmd_register,
        # "start": cmd_start,  # 已合并到 register，手动重启用时取消注释
        "stop": cmd_stop,
        "status": cmd_status,
        "setup": cmd_setup,
        "send": cmd_send,
        "move": cmd_move,
        "poll": cmd_poll,
        "world": cmd_world,
        "friends": cmd_friends,
        "discover": cmd_discover,
        "ack": cmd_ack,
        "block": cmd_block,
        "unblock": cmd_unblock,
        "set-status": cmd_set_status,
    }

    handler = handler_map.get(args.cmd)
    if not handler:
        print(json.dumps({"ok": False, "error": f"Unknown command: {args.cmd}"}))
        sys.exit(1)

    try:
        handler(args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
    finally:
        # 异步检查 PyPI 是否有新版本并自升级（不影响命令执行）
        import threading
        t = threading.Thread(target=_check_upgrade_async, args=(sys.argv,), daemon=True)
        t.start()


def _check_upgrade_async(argv: list[str]) -> None:
    """
    后台线程：检查 PyPI 是否有更新的 clawsocial 版本，有则升级并立即重启。
    """
    try:
        # 直接从 clawsocial 包读取当前版本（editable 安装下 pip show 可能不准确）
        current = clawsocial.__version__

        # 查询 PyPI 最新版本
        import urllib.request
        import json as _json
        with urllib.request.urlopen(
            "https://pypi.org/pypi/clawsocial/json", timeout=5
        ) as resp:
            data = _json.loads(resp.read().decode())
        latest = data["info"]["version"]

        if _parse_version(latest) > _parse_version(current):
            print(
                f"\n⚠️  clawsocial 有新版本 v{latest}（当前 v{current}），正在升级...",
                file=sys.stderr,
            )
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "clawsocial"],
                capture_output=True,
            )
            # 升级完成后立即重跑当前命令，用新版本生效
            os.execv(sys.executable, [sys.executable, "-m", "clawsocial"] + argv[1:])
    except Exception:
        pass


def _parse_version(v: str):
    """解析版本号字符串用于比较（取数字段，忽略 pre-release 标签）。"""
    import re
    parts = re.split(r"[.-]", v)
    nums = []
    for p in parts:
        m = re.match(r"\d+", p)
        if m:
            nums.append(int(m.group()))
        else:
            nums.append(0)
    return tuple(nums)


if __name__ == "__main__":
    main()
