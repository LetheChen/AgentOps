"""SkillRegistry — 启动时扫描 skills/ 目录，解析 SKILL.md frontmatter 构建元数据索引。

三层知识分离（v2.1 §三）：
- Skill（本模块）：通用操作文档，回答"怎么操作"，跨业务域
- Knowledge Base：领域知识 + 提示词模板，回答"具体怎么做"，按需查询
- Workflow yaml：执行编排实例，回答"执行什么"

设计要点：
- skill 文件不全量 inline 到 system_prompt（节省 token）
- system_prompt 只注入 metadata 列表（id + description + domain 标签）
- LLM 按需调 read_skill(skill_id) 加载完整 body
- _shared 域的 skill 所有 agent 可见；其他域只对匹配域的 agent 可见

参考：docs/architecture/DESIGN_architecture_refactor_v2.md §三 Skill 体系
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SkillMeta:
    """skill 的 metadata 摘要（不含完整 body，仅用于 LLM 路由选择）。"""
    id: str                    # "dag-ops"（来自目录名，唯一键）
    name: str                  # frontmatter name（人类可读）
    description: str           # frontmatter description（一句话说明）
    domain: str                # "_shared" / "video_production" / ...
    depends_on: list[str] = field(default_factory=list)  # 依赖的其他 skill id
    file_path: str = ""        # SKILL.md 绝对路径
    content: str = ""          # 完整 body（按需读取，启动时不加载）

    def __post_init__(self) -> None:
        # 兜底：id 为空时从 file_path 推断
        if not self.id and self.file_path:
            self.id = Path(self.file_path).parent.name


# 匹配 YAML frontmatter（--- ... ---）
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)$",
    re.DOTALL,
)


class SkillRegistry:
    """扫描 skills/ 目录，构建 skill metadata 索引。

    启动时调 scan()，运行时调 build_prompt_section() 生成 system_prompt 段。
    LLM 调 read_skill(skill_id) 时通过 get_skill_body(skill_id) 按需读取完整 body。
    """

    def __init__(self, skills_dir: str | Path | None = None):
        if skills_dir is None:
            project_root = Path(__file__).parent.parent
            skills_dir = project_root / "skills"
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, SkillMeta] = {}

    def scan(self) -> None:
        """扫描 skills/*/SKILL.md，解析 frontmatter 构建 metadata 索引。

        无效文件跳过并告警，不阻断启动。
        """
        self._skills.clear()
        if not self.skills_dir.exists():
            logger.warning("skills 目录不存在: %s", self.skills_dir)
            return

        # rglob 递归扫描所有 SKILL.md（支持子目录嵌套）
        for skill_md in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                meta = self._parse_skill_file(skill_md)
                if meta:
                    self._skills[meta.id] = meta
                    logger.debug("skill 注册: %s [%s]", meta.id, meta.domain)
            except Exception as e:
                logger.warning("跳过无效 skill 文件 %s: %s", skill_md, e)

        logger.info("SkillRegistry 扫描完成: %d 个 skill", len(self._skills))

    def _parse_skill_file(self, path: Path) -> SkillMeta | None:
        """解析单个 SKILL.md：frontmatter + body 分离。

        兼容两种 frontmatter 格式：
        - 设计文档标准：name / description / domain / depends_on
        - workflow-author 既有格式：name / description / version / category / triggers
          （category 视为 domain 的别名，triggers 暂不索引）
        """
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            logger.warning("skill 文件 %s 缺少 frontmatter，跳过", path)
            return None

        front_text = match.group("front")
        body = match.group("body").strip()
        front = _parse_simple_yaml(front_text)

        # id 优先从 frontmatter 取，否则从目录名取
        skill_id = front.get("id") or path.parent.name
        name = front.get("name", skill_id)
        description = front.get("description", "")
        # domain：优先 domain 字段，回退 category 字段，再回退 _shared
        domain = front.get("domain") or front.get("category") or "_shared"
        # depends_on：支持 list 或逗号分隔字符串
        depends_on = front.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [d.strip() for d in depends_on.split(",") if d.strip()]

        return SkillMeta(
            id=skill_id,
            name=name,
            description=description,
            domain=domain,
            depends_on=list(depends_on),
            file_path=str(path),
            content=body,
        )

    def list_for_agent(self, agent_domain: str) -> list[SkillMeta]:
        """返回 agent 可见的 skill 列表（_shared + 匹配域）。

        Args:
            agent_domain: agent 的业务域（如 "manager" / "video_production"）

        Returns:
            可见 skill 列表（按 id 排序）
        """
        visible = [
            s for s in self._skills.values()
            if s.domain == "_shared" or s.domain == agent_domain
        ]
        return sorted(visible, key=lambda s: s.id)

    def get_skill_body(self, skill_id: str) -> str | None:
        """按需读取完整 skill body（不全量 inline）。

        Args:
            skill_id: skill ID（如 "dag-ops"）

        Returns:
            完整 body 文本；skill 不存在返回 None
        """
        skill = self._skills.get(skill_id)
        if skill is None:
            return None
        # content 在 scan 时已加载到内存（避免每次 read_skill 都读磁盘）
        # skill body 通常 1-5KB，全量驻留内存可接受
        return skill.content

    def get(self, skill_id: str) -> SkillMeta | None:
        """获取 skill metadata。"""
        return self._skills.get(skill_id)

    def build_prompt_section(self, agent_domain: str | None = None) -> str:
        """生成 system_prompt 中的 skill metadata 列表段。

        只列 metadata（id + description + domain 标签），不全量 inline body。
        LLM 看到需要的 skill 时调 read_skill(skill_id) 加载完整内容。

        Args:
            agent_domain: agent 业务域；None 表示列全部 skill（调试用）

        Returns:
            system_prompt 段文本；无可见 skill 返回空字符串
        """
        if agent_domain:
            skills = self.list_for_agent(agent_domain)
        else:
            skills = sorted(self._skills.values(), key=lambda s: s.id)

        if not skills:
            return ""

        lines = [
            "## 可用 Skill（需要详细操作指引时调 read_skill 加载完整内容）",
            "",
        ]
        for s in skills:
            # 简短 description：取首行，截断到 80 字符
            desc = (s.description or "").strip().split("\n")[0]
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"- **{s.id}**: {desc} [{s.domain}]")

        return "\n".join(lines)

    @property
    def skills(self) -> dict[str, SkillMeta]:
        return dict(self._skills)


def _parse_simple_yaml(text: str) -> dict[str, object]:
    """极简 YAML frontmatter 解析（不引入 pyyaml 依赖）。

    支持的格式：
    - key: value（标量）
    - key: [a, b, c]（inline list）
    - key:（换行后 - item 列表）
    - key: "quoted value"（去引号）

    不支持嵌套对象、多行字符串等复杂结构（SKILL.md frontmatter 不需要）。
    """
    result: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # 列表项：- value
        if stripped.startswith("- ") and current_key is not None and current_list is not None:
            item = stripped[2:].strip()
            current_list.append(_strip_quotes(item))
            continue

        # key: value 或 key:
        if ":" in stripped:
            # 先 flush 上一个 list
            if current_list is not None and current_key is not None:
                result[current_key] = current_list
                current_list = None

            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            if not value:
                # 可能是 list 起始（下一行 - item）
                current_key = key
                current_list = []
            else:
                # inline list: [a, b, c]
                if value.startswith("[") and value.endswith("]"):
                    inner = value[1:-1].strip()
                    if inner:
                        items = [_strip_quotes(i.strip()) for i in inner.split(",")]
                        result[key] = items
                    else:
                        result[key] = []
                else:
                    result[key] = _strip_quotes(value)
                current_key = None
                current_list = None

    # flush 最后一个 list
    if current_list is not None and current_key is not None:
        result[current_key] = current_list

    return result


def _strip_quotes(s: str) -> str:
    """去除字符串两端的引号（单引号或双引号）。"""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s
