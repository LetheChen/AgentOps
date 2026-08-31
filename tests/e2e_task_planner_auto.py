"""自动链路 E2E：模拟 task_planner 经 3 工具驱动 idea→reviewing 停住等审批 + thread_id 透传。

对应设计文档 §4.9.5 task_planner 执行示例 + P0 验收点②⑧。
用法：python -m tests.e2e_task_planner_auto
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit.store import SqliteEventStore
from task.store import TaskStore
from task.orchestrator import TaskOrchestrator


async def main():
    # 复用与 server.py 相同的初始化路径
    store_conn = SqliteEventStore(db_path="audit.db", task_v1_enabled=False)
    task_store = TaskStore(store_conn._conn, store_conn._db_lock)
    orch = TaskOrchestrator(task_store, p0_mode=True)

    thread_id = "thread_e2e_auto_001"
    project_id = "proj_e2e_auto"

    # 0. 建项目
    await task_store.create_project(project_id=project_id, name="E2E自动链路项目", type="code")

    # ① task_submit_idea 工具（带 thread_id 透传）
    r1 = await orch.submit_idea(
        task_id="task_e2e_auto",
        project_id=project_id,
        title="自动链路验证任务",
        description="模拟 task_planner 自动驱动",
        risk_level="medium",
        thread_id=thread_id,
    )
    assert r1["ok"], f"submit_idea 失败: {r1}"
    task = r1["task"]
    tid = task["task_id"]
    print(f"[1] submit_idea OK: {task['identifier']} status={task['status']} v{task['version']} thread_id={task['thread_id']}")
    assert task["thread_id"] == thread_id, f"thread_id 透传失败: {task['thread_id']!r} != {thread_id!r}"

    v = r1["if_version"]

    # ② task_advance_stage: idea→backlog（agent 自动）
    r2 = await orch.advance_stage(task_id=tid, target_status="backlog", if_version=v, actor="agent", thread_id=thread_id)
    assert r2["ok"], f"idea→backlog 失败: {r2}"
    v = r2["if_version"]
    print(f"[2] idea→backlog OK: status={r2['task']['status']} v{v}")

    # ③ task_advance_stage: backlog→discussing（agent 自动）
    r3 = await orch.advance_stage(task_id=tid, target_status="discussing", if_version=v, actor="agent", thread_id=thread_id)
    assert r3["ok"], f"backlog→discussing 失败: {r3}"
    v = r3["if_version"]
    print(f"[3] backlog→discussing OK: status={r3['task']['status']} v{v}")

    # ④ task_advance_stage: discussing→reviewing（agent 自动）
    r4 = await orch.advance_stage(task_id=tid, target_status="reviewing", if_version=v, actor="agent", thread_id=thread_id)
    assert r4["ok"], f"discussing→reviewing 失败: {r4}"
    v = r4["if_version"]
    print(f"[4] discussing→reviewing OK: status={r4['task']['status']} v{v}")

    # ⑤ 关键断言：reviewing→closed 是 requires_user=True，agent 不可自动触发，应停住等审批
    r5 = await orch.advance_stage(task_id=tid, target_status="closed", if_version=v, actor="agent", thread_id=thread_id)
    assert not r5["ok"], f"agent 不应能关闭评审任务，但成功了: {r5}"
    assert r5["error"] == "requires_user_approval", f"错误码不符: {r5['error']}"
    print(f"[5] reviewing 停住等审批 OK: agent 被拒绝 ({r5['error']})")

    # ⑥ thread_id 持久化校验（从 DB 重读）
    fresh = await task_store.get_task(tid)
    assert fresh["thread_id"] == thread_id, f"DB thread_id 不符: {fresh['thread_id']!r}"
    assert fresh["status"] == "reviewing", f"状态应为 reviewing: {fresh['status']}"
    print(f"[6] thread_id 持久化 OK: DB status={fresh['status']} thread_id={fresh['thread_id']}")

    # ⑦ 用户审批后达 closed
    r7 = await orch.advance_stage(task_id=tid, target_status="closed", if_version=v, actor="user", thread_id=thread_id)
    assert r7["ok"], f"user 关闭失败: {r7}"
    assert r7["task"]["status"] == "closed", f"关闭后状态不符: {r7['task']['status']}"
    assert r7["task"]["closed_at"] is not None, "closed_at 未写入"
    print(f"[7] user 审批关闭 OK: status={r7['task']['status']} v{r7['if_version']} closed_at={r7['task']['closed_at']}")

    print("\n=== 自动链路 E2E 全部通过（验收点②⑧）===")
    print(f"  ② task_planner 3 工具驱动 idea→reviewing 停住等审批，用户审批后达 closed ✓")
    print(f"  ⑧ tasks 表含 thread_id 且自动路径透传写入 ({thread_id}) ✓")


if __name__ == "__main__":
    asyncio.run(main())
