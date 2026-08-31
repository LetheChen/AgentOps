"""bench runner 输出编码回归测试。"""

from bench.runner import render_markdown_report


def test_render_markdown_report_uses_readable_chinese_labels():
    report = render_markdown_report([], 3, "hello-world.yaml")

    expected_labels = (
        "# M0 选型 Benchmark 报告",
        "**测试时间**:",
        "**测试者**:",
        "**测试用例**:",
        "**候选运行次数**:",
        "## 指标矩阵",
        "| 启动耗时 (ms) |",
        "| Token 成本 (in+out) |",
        "| SDK breaking 频率 (6月内) |",
        "| 原生事件可观测性 |",
        "## 试阶详情",
        "## 已知问题",
        "3 candidates 全部 ok",
        "## 决策 (测试者手动填)",
        "**推荐**:",
        "**理由** (3 条):",
    )
    for label in expected_labels:
        assert label in report