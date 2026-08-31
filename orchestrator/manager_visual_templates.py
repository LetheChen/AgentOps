"""Manager visual templates — 预验证的 A2UI 模板生成器。

阶段 1（P0）：建立模板预验证机制。由于 A2UI 子组件数量由 data 动态决定
（metrics/rows/events/steps 数量可变），模板不能是纯静态 JSON，必须是
"生成器函数"——即 present_content.py 的 _map_* 函数。

本模块的职责：
1. load_template(content_type, data, tone) — 调 _map_* 生成 components + reachable 校验
2. pre_validate_all_templates() — 启动时用示例 data 预验证所有 _map_* 输出 reachable

阶段 2 时 present_content 将改为调 load_template 而非直接调 _map_*，
确保所有生成路径都经过 reachable 校验。

设计要点：view 模板预验证机制。
pinned view（固定组件数）可用静态 JSON；动态 content_type（metrics/rows/events 数量可变）必须用生成器函数。
"""
from __future__ import annotations

import logging
from typing import Any

from orchestrator.present_content import _MAP_FUNCTIONS, _map_dashboard, _map_form

logger = logging.getLogger(__name__)

# 从 report_surface_state import reachable 校验（单一真相源）
from tools.report_surface_state import _validate_reachable


# ── 示例 data（启动预验证用）──────────────────────────

PRE_VALIDATION_SAMPLES: dict[str, dict[str, Any]] = {
    "metric_group": {
        "metrics": [
            {"label": "总实例", "value": "12", "tone": "neutral"},
            {"label": "健康", "value": "10", "tone": "positive"},
            {"label": "告警", "value": "2", "tone": "critical"},
        ]
    },
    "table": {
        "columns": [
            {"id": "name", "label": "名称", "format": "text"},
            {"id": "cpu", "label": "CPU%", "format": "number"},
        ],
        "rows": [
            {"name": "web-01", "cpu": 45},
            {"name": "web-02", "cpu": 78},
        ],
    },
    "timeline": {
        "events": [
            {"time": "10:00", "title": "启动", "detail": "初始化", "tone": "info"},
            {"time": "10:05", "title": "完成", "detail": "done", "tone": "positive"},
        ]
    },
    "progress": {
        "percent": 75,
        "steps": [
            {"title": "扫描", "detail": "完成", "status": "done"},
            {"title": "报告", "detail": "进行中", "status": "active"},
        ],
    },
    "comparison": {
        "left": {"title": "方案 A", "items": [{"label": "成本", "value": "低"}]},
        "right": {"title": "方案 B", "items": [{"label": "成本", "value": "高"}]},
    },
    "dag_flow": {
        "nodes": [
            {"id": "scan", "title": "扫描", "status": "done"},
            {"id": "report", "title": "报告", "status": "pending", "depends_on": ["scan"]},
        ]
    },
    "disclosure_list": {
        "items": [
            {"title": "ERROR 1", "detail": "连接失败", "tone": "critical"},
            {"title": "WARN 2", "detail": "内存高", "tone": "warning"},
        ]
    },
    "bar_chart": {
        "items": [{"label": "API", "value": 1200}, {"label": "Web", "value": 800}],
        "unit": "QPS",
    },
    "line_chart": {
        "x_axis": ["08-13", "08-14", "08-15"],
        "series": [{"name": "最高温", "data": [32, 31, 30]}],
        "unit": "°C",
    },
    "pie_chart": {
        "items": [{"label": "搜索", "value": 45}, {"label": "直接", "value": 30}],
        "unit": "%",
    },
    "media": {
        "type": "image",
        "url": "https://example.com/output.png",
        "caption": "预览",
    },
    "form": {
        "fields": [
            {"name": "env", "label": "环境", "type": "select", "options": ["dev", "prod"]},
            {"name": "replicas", "label": "副本", "type": "number"},
        ],
    },
    "dashboard": {
        "panels": [
            {
                "title": "概览",
                "content_type": "metric_group",
                "data": {"metrics": [{"label": "CPU", "value": "45%"}]},
            },
            {
                "title": "趋势",
                "content_type": "line_chart",
                "data": {
                    "x_axis": ["08-13", "08-14"],
                    "series": [{"name": "temp", "data": [32, 31]}],
                },
            },
        ]
    },
}


# ── 加载函数 ──────────────────────────────────────────


def load_template(
    content_type: str,
    data: dict[str, Any],
    tone: str | None = None,
    actions: list | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """调 _map_* 生成 components 并校验 reachable。

    present_content / present_content_surface 均调本函数，确保所有生成路径
    都经过 reachable 校验（单一入口）。

    Args:
        content_type: 13 种之一（metric_group/table/.../dashboard）
        data: 业务数据（按 SKILL.md §4 schema）
        tone: 可选整体色调
        actions: 可选交互按钮（仅 form/table/disclosure_list/dashboard 用，
                 v2 后 form 即将废弃，但保留参数避免破坏现有调用）

    Returns:
        (components, content) — components 是 A2UI 组件树，content 是内联数据

    Raises:
        ValueError: content_type 未知或 reachable 校验失败
    """
    if content_type == "dashboard":
        components, content = _map_dashboard(data, tone, actions=actions)
    elif content_type == "form":
        components, content = _map_form(data, tone, actions=actions)
    else:
        map_fn = _MAP_FUNCTIONS.get(content_type)
        if map_fn is None:
            raise ValueError(f"unknown content_type: {content_type}")
        components, content = map_fn(data, tone)

    # reachable 校验（防止 dangling children 导致前端降级）
    issues = _validate_reachable(components)
    if issues:
        raise ValueError(
            f"template '{content_type}' generated unreachable components: {'; '.join(issues)}"
        )

    return components, content


# ── 启动预验证 ────────────────────────────────────────


def pre_validate_all_templates() -> dict[str, str]:
    """启动时用示例 data 预验证所有 _map_* 输出 reachable。

    在进程启动时调用（如 api/server.py 启动钩子），确保所有模板生成器
    的输出都通过 reachable 校验。若有模板生成 dangling children，
    启动时即暴露，而非等到运行时前端降级。

    Returns:
        {content_type: "ok" | "FAIL: <reason>"} — 预验证结果
    """
    results: dict[str, str] = {}
    for content_type, sample_data in PRE_VALIDATION_SAMPLES.items():
        try:
            components, _ = load_template(content_type, sample_data, tone="info")
            results[content_type] = f"ok (components={len(components)})"
            logger.info(
                "manager_visual_templates pre_validate ok: %s components=%d",
                content_type, len(components),
            )
        except Exception as e:
            results[content_type] = f"FAIL: {e}"
            logger.error(
                "manager_visual_templates pre_validate FAIL: %s — %s",
                content_type, e,
            )
    return results


if __name__ == "__main__":
    # 手动验证：python -m orchestrator.manager_visual_templates
    print("Pre-validating all manager visual templates...")
    results = pre_validate_all_templates()
    failed = [k for k, v in results.items() if v.startswith("FAIL")]
    for k, v in results.items():
        print(f"  {k}: {v}")
    if failed:
        print(f"\nFAILED: {failed}")
        exit(1)
    print("\nAll templates passed reachable validation.")
