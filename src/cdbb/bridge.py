"""
cdbb.bridge — 守护进程核心

架构说明
--------
外部有两条路径同时运行：

  Claude Code CLI
      │ PreToolUse hook（每次工具调用触发）
      ▼
  Unix Socket  (/tmp/cdbb.sock)
      │
      ▼
  Bridge（本模块）── BLE NUS ──► M5StickC Plus
      │                              │
      │◄─────── 按键决策（once/deny）──┘
      │
      ▼
  把 decision 写回 hook 进程 → hook 输出 CC 协议 JSON → Claude Code 继续

关键设计（借鉴 CharmYue/cc-buddy-bridge）
-----------------------------------------
1. permission_lock 串行化并发请求，第二个请求在第一个审批完成后才弹出。
2. EOF 竞争检测：若 hook 进程提前退出（用户 Esc / CC 超时），立即清空设备
   显示，而不是傻等 PERMISSION_TIMEOUT。
3. 心跳写入失败计数：连续 HEARTBEAT_FAIL_LIMIT 次失败后 os._exit(1)，
   由 launchd/systemd 重启（os._exit 绕过 asyncio 清理，避免死锁）。
4. Fail-open：bridge 不在线时 hook 退出码 0 且无输出，CC 走自己的对话框。

额外改进
--------
- 自动 BLE 扫描发现（无需手动填写 ADDR）
- 中文字符全部 sanitize（避免固件 5x7 点阵字体索引越界导致蓝牙栈重置）
- entries 顺序修正（固件期望最旧在前，hook 上报最新在前，此处 reversed）
- 支持 Linux（需 sudo setcap cap_net_raw+eip $(which python3)）
- 通过环境变量 CDBB_ADDR 固定地址（跳过扫描，加速启动）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakClient, BleakScanner

# ── BLE 常量（Nordic UART Service）────────────────────────────────────────────
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # 主机 → 设备（Write）
NUS_TX      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # 设备 → 主机（Notify）

DEVICE_NAME_PREFIX   = "Claude"       # 官方固件广播名前缀
SOCKET_PATH          = "/tmp/cdbb.sock"
HEARTBEAT_INTERVAL   = 3.0            # 秒，与官方桌面端保持一致
HEARTBEAT_FAIL_LIMIT = 5              # 连续失败次数超限后自退出
PERMISSION_TIMEOUT   = 110.0          # 秒，必须小于 CC hook 超时（120s）
ENTRIES_MAX          = 5              # 设备显示的历史条目上限

logger = logging.getLogger("cdbb.bridge")

# ── 中文 sanitize（固件 5×7 点阵字体只支持 ASCII）──────────────────────────────
_NON_ASCII = re.compile(r"[^\x00-\x7f]")

def sanitize(text: str, max_len: int = 60) -> str:
    """将非 ASCII 字符替换为 '?' 并截断，保护固件不崩溃。"""
    return _NON_ASCII.sub("?", text)[:max_len]


# ── 时区偏移（供设备时钟同步）────────────────────────────────────────────────
def _tz_offset_seconds() -> int:
    return -time.altzone if time.daylight and time.localtime().tm_isdst else -time.timezone


# ── 数据结构 ──────────────────────────────────────────────────────────────────
@dataclass
class PendingRequest:
    id: str
    tool: str
    hint: str
    decision_future: asyncio.Future


@dataclass
class BridgeState:
    """所有可观测状态集中在一处，方便序列化为 BLE 快照。"""
    pending: Optional[PendingRequest] = None
    entries: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        """生成发给设备的标准快照 payload。"""
        if self.pending is not None:
            return {
                "total": 1,
                "running": 0,
                "waiting": 1,
                "msg": sanitize(f"approve: {self.pending.tool}"),
                # 固件期望最旧在前 → reversed
                "entries": list(reversed(self.entries[:ENTRIES_MAX])),
                "tokens": 0,
                "tokens_today": 0,
                "prompt": {
                    "id": self.pending.id,
                    "tool": sanitize(self.pending.tool),
                    "hint": sanitize(self.pending.hint),
                },
            }
        return {
            "total": 0,
            "running": 0,
            "waiting": 0,
            "msg": "",
            "entries": list(reversed(self.entries[:ENTRIES_MAX])),
            "tokens": 0,
            "tokens_today": 0,
        }

    def push_entry(self, text: str) -> None:
        ts = time.strftime("%H:%M")
        self.entries.insert(0, f"{ts} {sanitize(text, 50)}")
        self.entries = self.entries[:ENTRIES_MAX]


# ── Bridge 主类 ───────────────────────────────────────────────────────────────
class Bridge:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.state = BridgeState()
        self._rx_buf = bytearray()
        self._tx_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._permission_lock = asyncio.Lock()

    # ── BLE 收发 ───────────────────────────────────────────────────────────────

    def on_notify(self, _sender: int, data: bytearray) -> None:
        """设备 → 主机：累积分包，按行解析。"""
        self._rx_buf.extend(data)
        while True:
            nl = self._rx_buf.find(b"\n")
            if nl < 0:
                return
            line = bytes(self._rx_buf[:nl])
            del self._rx_buf[: nl + 1]
            if not line.strip():
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception as e:
                logger.warning("设备消息解析失败: %r — %s", line, e)
                continue
            logger.debug("设备 → 主机: %s", json.dumps(obj, ensure_ascii=False))
            self._tx_queue.put_nowait(obj)

    async def send(self, obj: dict) -> None:
        """主机 → 设备：序列化为 JSON 行，Write With Response 发出。"""
        payload = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        async with self._write_lock:
            await self.client.write_gatt_char(NUS_RX, payload, response=True)

    async def push_snapshot(self) -> None:
        await self.send(self.state.snapshot())

    # ── 后台任务 ───────────────────────────────────────────────────────────────

    async def heartbeat_loop(self) -> None:
        """每 HEARTBEAT_INTERVAL 秒推送快照；连续失败则自退出让 supervisor 重启。"""
        consecutive_failures = 0
        while True:
            try:
                await self.push_snapshot()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    "心跳写入失败 (%d/%d): %s",
                    consecutive_failures, HEARTBEAT_FAIL_LIMIT, e,
                )
                if consecutive_failures >= HEARTBEAT_FAIL_LIMIT:
                    logger.error("BLE 链路已死，退出等待重启…")
                    # os._exit 绕过 asyncio 清理，避免 wedged BleakClient 死锁
                    os._exit(1)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def tx_dispatcher(self) -> None:
        """处理设备发来的消息，当前只关心 permission 决策。"""
        while True:
            msg = await self._tx_queue.get()
            cmd = msg.get("cmd")

            if cmd == "permission":
                # 先 ack，让设备清除 UI，再 resolve future
                try:
                    await self.send({"ack": "permission", "ok": True, "n": 0})
                except Exception as e:
                    logger.warning("permission ack 发送失败: %s", e)

                mid = msg.get("id")
                decision = msg.get("decision")
                pending = self.state.pending

                if pending and pending.id == mid:
                    if not pending.decision_future.done():
                        pending.decision_future.set_result(decision)
                else:
                    logger.warning("收到孤立 permission id=%r decision=%r", mid, decision)

    # ── Hook 客户端处理 ────────────────────────────────────────────────────────

    async def handle_hook_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        处理来自 hook.py 的单次连接。

        时序：
          1. 读取请求 JSON（含 id / tool / hint）
          2. 获取 permission_lock（串行化并发请求）
          3. 推送快照给设备，设备 UI 亮起
          4. 同时等待：(a) 设备按键决策 或 (b) hook 进程 socket EOF
             — 先到先得，避免 hook 被 CC 提前 kill 后设备一直亮着
          5. 把 decision 写回 hook 进程
        """
        peer = writer.get_extra_info("peername") or "?"
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] 5 秒内未收到请求行", peer)
            writer.close()
            return

        if not raw:
            writer.close()
            return

        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.warning("[%s] JSON 解析失败: %s raw=%r", peer, e, raw)
            writer.write((json.dumps({"decision": "error", "error": "bad_json"}) + "\n").encode())
            await writer.drain()
            writer.close()
            return

        rid  = str(req.get("id")   or f"req_{int(time.time() * 1000)}")
        tool = str(req.get("tool") or "?")
        hint = str(req.get("hint") or "")

        logger.info("收到请求 id=%s tool=%s hint=%r", rid, tool, hint)

        async with self._permission_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.state.pending = PendingRequest(id=rid, tool=tool, hint=hint, decision_future=fut)
            self.state.push_entry(f"{tool}: {hint}")

            try:
                await self.push_snapshot()
            except Exception as e:
                logger.warning("快照推送失败: %s", e)

            # 竞争：设备决策 vs hook 进程提前退出（EOF）
            decision_task = asyncio.create_task(
                asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT),
                name=f"decision:{rid}",
            )
            eof_task = asyncio.create_task(reader.read(1), name=f"eof:{rid}")

            done, _ = await asyncio.wait(
                {decision_task, eof_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            client_gone = False

            if decision_task in done:
                eof_task.cancel()
                try:
                    decision = decision_task.result()
                except asyncio.TimeoutError:
                    decision = "timeout"
                    logger.warning("id=%s 审批超时，默认拒绝", rid)
            else:
                # hook 进程先退出 → 立即清空设备显示
                client_gone = True
                decision = "abandoned"
                decision_task.cancel()
                logger.info("id=%s hook 进程已离开，清空设备显示", rid)

            self.state.pending = None

            try:
                await self.push_snapshot()
            except Exception as e:
                logger.warning("清空快照推送失败: %s", e)

        logger.info("id=%s → decision=%s", rid, decision)

        if not client_gone:
            try:
                writer.write((json.dumps({"decision": decision}) + "\n").encode())
                await writer.drain()
            except Exception as e:
                logger.warning("写回决策失败: %s", e)

        try:
            writer.close()
        except Exception:
            pass


# ── BLE 扫描与连接 ────────────────────────────────────────────────────────────

async def find_device() -> str:
    """扫描并返回第一个以 'Claude' 开头的设备地址。"""
    env_addr = os.environ.get("CDBB_ADDR", "").strip()
    if env_addr:
        logger.info("使用环境变量 CDBB_ADDR=%s（跳过扫描）", env_addr)
        return env_addr

    logger.info("正在扫描 BLE 设备（广播名前缀：%s）…", DEVICE_NAME_PREFIX)
    device = await BleakScanner.find_device_by_filter(
        lambda d, _ad: bool(d.name and d.name.startswith(DEVICE_NAME_PREFIX)),
        timeout=15.0,
    )
    if device is None:
        raise RuntimeError(
            f"未找到名称以 '{DEVICE_NAME_PREFIX}' 开头的 BLE 设备。\n"
            "  • 确认设备已开机且蓝牙已启用\n"
            "  • 或通过 CDBB_ADDR=<地址> 环境变量手动指定"
        )
    logger.info("发现设备: %s  地址: %s", device.name, device.address)
    return device.address


# ── 主入口 ─────────────────────────────────────────────────────────────────────

async def run() -> None:
    addr = await find_device()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        logger.info("收到退出信号，正在关闭…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    logger.info("正在连接 %s …", addr)
    async with BleakClient(addr, timeout=15.0) as client:
        logger.info("已连接，MTU=%d", client.mtu_size)
        bridge = Bridge(client)

        await client.start_notify(NUS_TX, bridge.on_notify)
        logger.info("已订阅设备通知")

        # 同步设备时钟
        await bridge.send({"time": [int(time.time()), _tz_offset_seconds()]})

        server = await asyncio.start_unix_server(
            bridge.handle_hook_client, path=SOCKET_PATH
        )
        os.chmod(SOCKET_PATH, 0o600)
        logger.info("Unix Socket 监听中: %s", SOCKET_PATH)

        hb_task   = asyncio.create_task(bridge.heartbeat_loop(),  name="heartbeat")
        tx_task   = asyncio.create_task(bridge.tx_dispatcher(),   name="tx_dispatcher")
        srv_task  = asyncio.create_task(server.serve_forever(),   name="unix_server")
        stop_task = asyncio.create_task(stop_event.wait(),        name="stop_wait")

        logger.info("claude-desktop-buddy-bridge 守护进程已就绪 ✓")

        done, pending = await asyncio.wait(
            {hb_task, tx_task, srv_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()

        for t in done:
            if t is not stop_task and not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error("任务 %s 异常退出: %r", t.get_name(), exc)

        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass

        try:
            await client.stop_notify(NUS_TX)
        except Exception:
            pass

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    logger.info("claude-desktop-buddy-bridge 已退出")
