"""claude-desktop-buddy-bridge 单元测试"""

import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cdbb.bridge import BridgeState, Bridge, sanitize, ENTRIES_MAX


# ── sanitize ──────────────────────────────────────────────────────────────────

def test_sanitize_ascii_passthrough():
    assert sanitize("rm -rf /tmp/foo") == "rm -rf /tmp/foo"

def test_sanitize_replaces_cjk():
    result = sanitize("删除文件 /tmp/foo")
    assert "?" in result
    assert "/tmp/foo" in result
    # 不含任何非 ASCII 字节（保护固件）
    assert result.isascii()

def test_sanitize_truncates():
    long_text = "a" * 200
    assert len(sanitize(long_text, max_len=60)) == 60

def test_sanitize_mixed():
    result = sanitize("git push origin main — 稳云运维", max_len=100)
    assert result.isascii()
    assert "git push" in result


# ── BridgeState ───────────────────────────────────────────────────────────────

def test_snapshot_idle():
    state = BridgeState()
    snap = state.snapshot()
    assert snap["waiting"] == 0
    assert snap["running"] == 0
    assert "prompt" not in snap

def test_snapshot_with_pending():
    state = BridgeState()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()

    from cdbb.bridge import PendingRequest
    state.pending = PendingRequest(
        id="req_001", tool="Bash", hint="rm -rf /tmp", decision_future=fut
    )
    snap = state.snapshot()
    assert snap["waiting"] == 1
    assert snap["prompt"]["id"] == "req_001"
    assert snap["prompt"]["tool"] == "Bash"
    # hint 是 ASCII 安全的
    assert snap["prompt"]["hint"].isascii()
    loop.close()

def test_snapshot_entries_reversed():
    """固件期望最旧条目在前，BridgeState 内部存储最新在前，snapshot 需要 reversed。"""
    state = BridgeState()
    state.push_entry("first")
    state.push_entry("second")
    state.push_entry("third")
    snap = state.snapshot()
    entries = snap["entries"]
    # 最后 push 的 "third" 在 entries[0]（内部最新在前）
    # reversed 后 entries[-1] 应该含有 "third"
    assert any("third" in e for e in entries[-1:])
    assert any("first" in e for e in entries[:1])

def test_push_entry_capped():
    state = BridgeState()
    for i in range(ENTRIES_MAX + 3):
        state.push_entry(f"entry {i}")
    assert len(state.entries) == ENTRIES_MAX

def test_push_entry_sanitized():
    state = BridgeState()
    state.push_entry("部署完成 — deploy done")
    assert state.entries[0].isascii()


# ── hook.py 逻辑 ──────────────────────────────────────────────────────────────

def test_make_hint_command():
    from cdbb.hook import _make_hint
    assert _make_hint({"command": "ls -la", "other": "ignored"}) == "ls -la"

def test_make_hint_file_path():
    from cdbb.hook import _make_hint
    assert _make_hint({"file_path": "/etc/hosts"}) == "/etc/hosts"

def test_make_hint_fallback_json():
    from cdbb.hook import _make_hint
    result = _make_hint({"unknown_key": "value"})
    assert "unknown_key" in result

def test_make_hint_non_dict():
    from cdbb.hook import _make_hint
    assert _make_hint("raw string") == "raw string"

def test_make_hint_truncated():
    from cdbb.hook import _make_hint, HINT_MAX
    long_val = "x" * 300
    assert len(_make_hint({"command": long_val})) == HINT_MAX
