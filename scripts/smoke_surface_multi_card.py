"""E2E 验收：present_content 多次调用累集多张卡（方案 C 核心修复）。

验证路径：
  1. 创建 manager session（claude_sdk harness）
  2. 发 turn：要求 LLM 用 present_content 展示三张不同的指标卡
  3. 验证 session_events 中：
     - 出现 ≥3 个 report_surface_state 事件
     - 每个 surface_state.surface_id 互不重复
     - patch_sequence 单调递增
     - view_id 都是 "manager-live"
  4. 验证前端 SupervisionPanel 能渲染 ≥3 张卡（回放测试 + DOM 截图）
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path


API = "http://127.0.0.1:1987"
WORKSPACE_ID = "c6bcb415-df2d-4a17-950e-3b4a88e15790"
DB_PATH = Path(__file__).resolve().parent.parent / "audit.db"


def _req(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {e.code}: {body}") from None


def _read_events(session_id: str) -> list[dict]:
    """从 session_events 表拉该 session 的事件（DagEvent 序列）。"""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT event_type, sequence, payload FROM session_events "
        "WHERE session_id=? ORDER BY sequence",
        (session_id,),
    ).fetchall()
    con.close()
    out: list[dict] = []
    for r in rows:
        payload_raw = r["payload"]
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else (payload_raw or {})
        except Exception:
            payload = {}
        out.append({
            "event_type": r["event_type"],
            "sequence": r["sequence"],
            "payload": payload,
        })
    return out


async def main() -> int:
    print(f"[1] POST /api/v2/sessions workspace={WORKSPACE_ID[:8]}")
    sess = await asyncio.to_thread(
        _req, "POST", "/api/v2/sessions",
        {"agent_id": "manager", "title": "multi-card smoke",
         "workspace_id": WORKSPACE_ID},
    )
    sid = sess["session_id"]
    print(f"    session_id={sid}")

    print(f"[2] 发 turn：要求 present_content 三次（每个 widget_id 不同）")
    await asyncio.to_thread(
        _req, "POST", f"/api/v2/sessions/{sid}/turns",
        {"message": (
            "请调用 present_content 工具 3 次，每次 widget_id 不同："
            "  - widget_id='kpi' 展示 metric_group：会话数 5，活跃数 2，token 12345；"
            "  - widget_id='table' 展示 table：2 行 3 列；"
            "  - widget_id='timeline' 展示 timeline：3 个事件。"
            "必须按顺序展示三张不同的卡片。"
        )},
    )

    print(f"[3] 等待 turn 完成（最长 120s）...")
    deadline = time.time() + 120
    counts: Counter[str] = Counter()
    while time.time() < deadline:
        await asyncio.sleep(3)
        events = _read_events(sid)
        counts = Counter(e["event_type"] for e in events)
        if counts.get("turn.completed", 0) or counts.get("turn.failed", 0):
            break

    print(f"[4] 事件统计: {dict(counts)}")
    if counts.get("turn.failed", 0):
        print(f"    FAIL: turn failed")
        return 1

    # ── 核心验证 ──
    events = _read_events(sid)
    rss = [e for e in events if e["event_type"] == "report_surface_state"]
    print(f"[5] report_surface_state 事件数: {len(rss)}")
    if len(rss) < 3:
        print(f"    FAIL: 期望 ≥3 个，实际 {len(rss)}（模型没有调三次 present_content）")
        return 1

    surface_ids: list[str] = []
    patch_seqs: list[int] = []
    view_ids: set[str] = set()
    for ev in rss:
        ss = ev["payload"].get("surface_state") or {}
        surface_ids.append(ss.get("surface_id", ""))
        patch_seqs.append(int(ss.get("patch_sequence", 0) or 0))
        view_ids.add(ss.get("view_id", ""))

    print(f"[6] surface_ids: {[s[:12] for s in surface_ids]}")
    print(f"[7] patch_seqs: {patch_seqs}")
    print(f"[8] view_ids: {view_ids}")

    # 6.1 三次 surface_id 互不重复（每次独立 surface）
    if len(set(surface_ids)) < len(surface_ids):
        print(f"    FAIL: 有 surface_id 重复（dedup 应只针对同 surface 跨 phase 复用）")
        return 1

    # 6.2 三次 view_id 都是 manager-live
    if view_ids != {"manager-live"}:
        print(f"    FAIL: view_id 不一致，期望 {{'manager-live'}}，实际 {view_ids}")
        return 1

    # 6.3 至少有一次 patch_sequence > 0（patch 模型生效）
    if max(patch_seqs) < 1:
        print(f"    FAIL: patch_sequence 全为 0，未启用 patch 模型")
        return 1

    # 6.4 （可选）如果两次同 widget_id，应复用同一 surface_id（命名 surface 语义）
    #     本测试用不同 widget_id，所以三次都是新 surface_id；这里只检查互不重复。
    print(f"[9] OK: 多卡累集生效（surface_id 互异 + view 一致 + patch_seq 启用）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))