"""WorkflowRegistry — 启动时扫描 workflows/*.yaml，提取 metadata 用于 system_prompt 动态注入。

替代在 manager.yaml system_prompt 中硬编码 workflow 路由表的做法。
新增 workflow 只需放 yaml 文件到 workflows/ 目录，无需改 agent 配置。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from workflow.loader import load_workflow_yaml

logger = logging.getLogger(__name__)


@dataclass
class WorkflowMeta:
    """workflow 的 metadata 摘要（不含完整拓扑，仅用于 LLM 路由选择）。"""
    workflow_id: str
    name: str
    description: str
    node_count: int
    inputs_schema: list[dict] = field(default_factory=list)
    file_path: str = ""


class WorkflowRegistry:
    """扫描 workflows/ 目录，构建 workflow metadata 索引。

    启动时调用 scan()，运行时调 build_prompt_section() 生成 system_prompt 段。
    """

    def __init__(self, workflows_dir: str | Path | None = None):
        if workflows_dir is None:
            project_root = Path(__file__).parent.parent
            workflows_dir = project_root / "workflows"
        self.workflows_dir = Path(workflows_dir)
        self._workflows: dict[str, WorkflowMeta] = {}

    def scan(self) -> None:
        """扫描 workflows/*.yaml，提取 metadata。无效文件跳过并告警。"""
        self._workflows.clear()
        if not self.workflows_dir.exists():
            logger.warning("workflows 目录不存在: %s", self.workflows_dir)
            return

        for yaml_file in sorted(self.workflows_dir.glob("*.yaml")):
            try:
                wf = load_workflow_yaml(str(yaml_file))
                meta = WorkflowMeta(
                    workflow_id=wf.workflow_id,
                    name=wf.name,
                    description=wf.description or "",
                    node_count=len(wf.nodes),
                    inputs_schema=wf.inputs or [],
                    file_path=str(yaml_file),
                )
                self._workflows[meta.workflow_id] = meta
                logger.debug("workflow 注册: %s (%d nodes)", meta.workflow_id, meta.node_count)
            except Exception as e:
                logger.warning("跳过无效 workflow %s: %s", yaml_file.name, e)

        logger.info("WorkflowRegistry 扫描完成: %d 个 workflow", len(self._workflows))

    def build_prompt_section(self) -> str:
        """生成 system_prompt 中的 workflow 注册表段。

        替代 manager.yaml 中硬编码的路由表，新增 workflow 无需改 agent 配置。
        """
        if not self._workflows:
            return "## 可用工作流\n（无）"

        lines = ["## 可用工作流（启动时自动扫描，新增 workflow 放 yaml 到 workflows/ 即可）", ""]
        for wf in sorted(self._workflows.values(), key=lambda w: w.workflow_id):
            lines.append(f"### {wf.workflow_id}")
            lines.append(f"- **名称**: {wf.name}")
            # description 可能是多行，取首行
            desc = wf.description.strip().split("\n")[0] if wf.description else ""
            lines.append(f"- **描述**: {desc}")
            lines.append(f"- **节点数**: {wf.node_count}")
            if wf.inputs_schema:
                lines.append("- **输入参数**:")
                for param in wf.inputs_schema:
                    pname = param.get("name", "?")
                    ptype = param.get("type", "any")
                    req = "必填" if param.get("required") else "可选"
                    pdesc = param.get("description", "").strip().split("\n")[0]
                    lines.append(f"  - `{pname}` ({ptype}, {req}): {pdesc}")
            lines.append("")
        return "\n".join(lines)

    def get(self, workflow_id: str) -> WorkflowMeta | None:
        return self._workflows.get(workflow_id)

    @property
    def workflows(self) -> dict[str, WorkflowMeta]:
        return dict(self._workflows)
