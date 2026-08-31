# scripts/gen_global_revision_triggers.py
"""生成 _TASK_SCHEMA_V1 常量尾部的 global_revision 触发器（14 表 × 3 操作 = 42 个）。

用法：python scripts/gen_global_revision_triggers.py
  打印到 stdout，开发者复制粘贴到 audit/store.py 的 _TASK_SCHEMA_V1 常量尾部
  （-- >>> GEN_TRIGGERS_BEGIN ... -- >>> GEN_TRIGGERS_END 标记段内）。
  不再写入独立 .sql 文件（对齐现有 _SCHEMA 内联模式）。
"""
from __future__ import annotations
import sys

# V1 新增的 14 张表（P0 的 projects/task_stages 已在 _TASK_SCHEMA_P0 内联，不重复）
# tasks 触发器幂等（IF NOT EXISTS），P0 已建则跳过
V1_TABLES = [
    "ideas", "task_relations", "task_events", "task_activities",
    "task_artifacts", "task_reports", "task_comments", "acceptance_criteria",
    "design_docs", "doc_change_proposals", "design_doc_changes",
    "agent_styles", "task_runs", "tasks",
]
OPS = ("insert", "update", "delete")
MARKER_BEGIN = "-- >>> GEN_TRIGGERS_BEGIN (auto-generated, do not edit)"
MARKER_END = "-- >>> GEN_TRIGGERS_END"

TEMPLATE = (
    "CREATE TRIGGER IF NOT EXISTS {tbl}_rev_after_{op} AFTER {OP} ON {tbl}\n"
    "BEGIN UPDATE global_revision SET revision = revision + 1 WHERE singleton = 1; END;"
)


def render() -> str:
    lines = [MARKER_BEGIN]
    for tbl in V1_TABLES:
        for op in OPS:
            lines.append(TEMPLATE.format(tbl=tbl, op=op, OP=op.upper()))
    lines.append(MARKER_END)
    return "\n".join(lines)


if __name__ == "__main__":
    print(render())
    print(f"\n# 共 {len(V1_TABLES) * 3} 个触发器，粘贴到 audit/store.py 的 _TASK_SCHEMA_V1 尾部", file=sys.stderr)
