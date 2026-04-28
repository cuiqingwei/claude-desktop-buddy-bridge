"""
cdbb.cli — 命令行入口

用法:
  cdbb daemon          启动守护进程（连接 BLE 设备，监听 Unix Socket）
  cdbb scan            扫描附近的 Claude BLE 设备并打印地址
  cdbb install         自动注入 Claude Code hook 配置
  cdbb uninstall       移除 Claude Code hook 配置
  cdbb status          检查守护进程是否在线
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

from cdbb import __version__


# ── 日志配置 ──────────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ── 子命令：daemon ─────────────────────────────────────────────────────────────

def cmd_daemon(args: argparse.Namespace) -> None:
    _setup_logging(args.verbose)
    from cdbb.bridge import run
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


# ── 子命令：scan ──────────────────────────────────────────────────────────────

def cmd_scan(_args: argparse.Namespace) -> None:
    _setup_logging()

    async def _scan() -> None:
        from bleak import BleakScanner
        print("正在扫描 BLE 设备（10 秒）…\n")
        devices = await BleakScanner.discover(timeout=10.0)
        found = False
        for d in sorted(devices, key=lambda x: x.name or ""):
            marker = " ◀ cdbb 兼容" if (d.name or "").startswith("Claude") else ""
            print(f"  {d.address}  {d.name or '(无名称)'}{marker}")
            if marker:
                found = True
        if not found:
            print("\n未发现 Claude 兼容设备。请确认设备已开机且在蓝牙范围内。")
        else:
            print(f"\n提示: 用 CDBB_ADDR=<地址> cdbb daemon 跳过扫描直接连接")

    asyncio.run(_scan())


# ── 子命令：status ─────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> None:
    from cdbb.bridge import SOCKET_PATH
    sock_path = Path(SOCKET_PATH)

    if not sock_path.exists():
        print("● cdbb 守护进程：未运行（socket 文件不存在）")
        sys.exit(1)

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCKET_PATH)
        s.close()
        print("● cdbb 守护进程：运行中 ✓")
    except (ConnectionRefusedError, socket.timeout, OSError):
        print("● cdbb 守护进程：socket 存在但无响应（可能已崩溃）")
        sys.exit(1)


# ── 子命令：install ────────────────────────────────────────────────────────────

def cmd_install(args: argparse.Namespace) -> None:
    _setup_logging()

    hook_script = Path(sys.executable).parent / "cdbb-hook"
    # 如果是 uv 安装，尝试找到 hook.py 的绝对路径
    hook_py = Path(__file__).parent / "hook.py"

    # 优先用已安装的 entry point，回退到直接调用 hook.py
    if hook_script.exists():
        command = str(hook_script)
    else:
        command = f"{sys.executable} {hook_py}"

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有配置
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            print(f"警告：无法解析 {settings_path}，将创建新文件")

    # 构造 hook 配置
    hook_entry = {
        "type": "command",
        "command": command,
        "timeout": 120,
    }

    # 决定 matcher：默认覆盖所有工具，可通过 --tools 限定
    matchers = args.tools if args.tools else [""]  # "" = 匹配所有

    hooks_block = existing.setdefault("hooks", {})
    permission_hooks = hooks_block.setdefault("PermissionRequest", [])

    # 检查是否已存在 cdbb 条目
    already = any(
        h.get("command", "").find("claude-desktop-buddy-bridge") >= 0
        for entry in permission_hooks
        for h in entry.get("hooks", [])
    )
    if already and not args.force:
        print("cdbb hook 已存在，无需重复安装。使用 --force 强制覆盖。")
        return

    # 移除旧条目后追加新条目
    permission_hooks[:] = [
        e for e in permission_hooks
        if not any(h.get("command", "").find("claude-desktop-buddy-bridge") >= 0 for h in e.get("hooks", []))
    ]

    for matcher in matchers:
        entry: dict = {"hooks": [hook_entry]}
        if matcher:
            entry["matcher"] = matcher
        permission_hooks.append(entry)

    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"✓ cdbb hook 已写入 {settings_path}")
    print(f"  命令: {command}")
    print(f"  覆盖范围: {'所有工具' if not args.tools else ', '.join(args.tools)}")
    print()
    print("下一步：运行 'cdbb daemon' 启动守护进程，然后开启 Claude Code。")


# ── 子命令：uninstall ─────────────────────────────────────────────────────────

def cmd_uninstall(_args: argparse.Namespace) -> None:
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print("未找到 Claude Code 配置文件，无需操作。")
        return

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读取配置失败: {e}")
        sys.exit(1)

    hooks_block = data.get("hooks", {})
    permission_hooks = hooks_block.get("PermissionRequest", [])

    before = len(permission_hooks)
    permission_hooks[:] = [
        e for e in permission_hooks
        if not any(h.get("command", "").find("claude-desktop-buddy-bridge") >= 0 for h in e.get("hooks", []))
    ]
    after = len(permission_hooks)

    if before == after:
        print("未找到 cdbb hook 条目，无需操作。")
        return

    settings_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"✓ 已移除 {before - after} 条 cdbb hook（{settings_path}）")


# ── 参数解析 ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-desktop-buddy-bridge",
        description="claude-desktop-buddy-bridge — Claude Code CLI ↔ BLE 物理审批按钮",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  cdbb scan                    # 扫描附近 BLE 设备
  cdbb install                 # 注入 hook（覆盖所有工具）
  cdbb install --tools Bash    # 只拦截 Bash 工具
  cdbb daemon                  # 启动守护进程
  cdbb daemon -v               # 调试模式（显示详细日志）
  cdbb status                  # 检查守护进程是否在线
  cdbb uninstall               # 移除 hook
  CDBB_ADDR=XX:XX:XX:XX cdbb daemon   # 跳过扫描
""",
    )
    parser.add_argument("-V", "--version", action="version", version=f"claude-desktop-buddy-bridge {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # daemon
    p_daemon = sub.add_parser("daemon", help="启动 BLE 守护进程")
    p_daemon.add_argument("-v", "--verbose", action="store_true", help="显示详细日志")
    p_daemon.set_defaults(func=cmd_daemon)

    # scan
    p_scan = sub.add_parser("scan", help="扫描附近 Claude BLE 设备")
    p_scan.set_defaults(func=cmd_scan)

    # status
    p_status = sub.add_parser("status", help="检查守护进程是否在线")
    p_status.set_defaults(func=cmd_status)

    # install
    p_install = sub.add_parser("install", help="自动注入 Claude Code hook 配置")
    p_install.add_argument(
        "--tools", nargs="+", metavar="TOOL",
        help="限定拦截的工具名（默认拦截所有工具）。例: --tools Bash Write",
    )
    p_install.add_argument("--force", action="store_true", help="强制覆盖已有配置")
    p_install.set_defaults(func=cmd_install)

    # uninstall
    p_uninstall = sub.add_parser("uninstall", help="移除 Claude Code hook 配置")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
