"""SkillRegistry + read_skill 工具测试 — Phase 2 Skill 体系验收。

覆盖：
- SkillRegistry.scan() 解析 frontmatter（标准格式 + 旧格式兼容）
- list_for_agent 域级过滤（_shared + 匹配域）
- get_skill_body 按需加载
- build_prompt_section 生成 system_prompt 段
- read_skill 工具 handler（正常 + 错误路径）
- _parse_simple_yaml 边界情况（标量 / inline list / block list / 引号字符串）
"""
from __future__ import annotations

import pytest

from orchestrator.skill_registry import (
    SkillMeta,
    SkillRegistry,
    _parse_simple_yaml,
    _strip_quotes,
)


# ──────────────────────────────────────────────────────────────────────────
# 测试 fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def skills_dir(tmp_path):
    """构造临时 skills/ 目录，含 3 个测试 skill。"""
    # _shared 域 skill（所有 agent 可见）
    shared_dir = tmp_path / "dag-ops"
    shared_dir.mkdir()
    (shared_dir / "SKILL.md").write_text(
        "---\n"
        "name: dag-ops\n"
        'description: DAG 操作指南\n'
        "domain: _shared\n"
        "depends_on: []\n"
        "---\n\n"
        "# DAG 操作指南\n\n触发工作流的方法...",
        encoding="utf-8",
    )

    # video_production 域 skill（只对 video 域 agent 可见）
    video_dir = tmp_path / "video-edit"
    video_dir.mkdir()
    (video_dir / "SKILL.md").write_text(
        "---\n"
        "name: video-edit\n"
        'description: 视频编辑技巧\n'
        "domain: video_production\n"
        "---\n\n"
        "# 视频编辑\n\n剪辑规范...",
        encoding="utf-8",
    )

    # 旧格式 skill（category 作为 domain 别名 + triggers 字段）
    legacy_dir = tmp_path / "workflow-author"
    legacy_dir.mkdir()
    (legacy_dir / "SKILL.md").write_text(
        "---\n"
        "name: workflow-author\n"
        'description: 生成 workflow yaml\n'
        "version: 1.0\n"
        "category: _shared\n"
        "depends_on: [dag-patterns]\n"
        "triggers:\n"
        "  - 创建 workflow\n"
        "  - 生成 DAG\n"
        "---\n\n"
        "# Workflow Author\n\n规范...",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def registry(skills_dir):
    """已扫描的 SkillRegistry 实例。"""
    r = SkillRegistry(skills_dir)
    r.scan()
    return r


# ──────────────────────────────────────────────────────────────────────────
# SkillRegistry.scan() + frontmatter 解析
# ──────────────────────────────────────────────────────────────────────────

def test_scan_parses_all_skills(registry):
    """scan() 解析全部 3 个 skill。"""
    assert len(registry.skills) == 3
    assert {"dag-ops", "video-edit", "workflow-author"} <= set(registry.skills.keys())


def test_scan_parses_standard_frontmatter(registry):
    """标准 frontmatter（name/description/domain/depends_on）正确解析。"""
    skill = registry.get("dag-ops")
    assert skill is not None
    assert skill.name == "dag-ops"
    assert skill.description == "DAG 操作指南"
    assert skill.domain == "_shared"
    assert skill.depends_on == []


def test_scan_parses_legacy_frontmatter_with_category(registry):
    """旧格式 frontmatter（category 作为 domain 别名）正确解析。"""
    skill = registry.get("workflow-author")
    assert skill is not None
    assert skill.name == "workflow-author"
    assert skill.domain == "_shared"  # category 回退为 domain
    assert skill.depends_on == ["dag-patterns"]


def test_scan_parses_domain_specific_skill(registry):
    """video_production 域 skill 正确解析。"""
    skill = registry.get("video-edit")
    assert skill is not None
    assert skill.domain == "video_production"


def test_scan_loads_body_content(registry):
    """scan() 时 body 已加载到内存（不全量 inline 但按需读取时不读磁盘）。"""
    body = registry.get_skill_body("dag-ops")
    assert body is not None
    assert "触发工作流的方法" in body


def test_scan_skips_invalid_frontmatter(tmp_path):
    """缺少 frontmatter 的文件被跳过，不阻断启动。"""
    bad_dir = tmp_path / "bad-skill"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text(
        "# 没有 frontmatter 的 skill\n\n直接 markdown 内容",
        encoding="utf-8",
    )
    r = SkillRegistry(tmp_path)
    r.scan()
    assert len(r.skills) == 0


def test_scan_id_from_directory_name(tmp_path):
    """frontmatter 无 id 字段时，从目录名推断 id。"""
    custom_dir = tmp_path / "my-custom-skill"
    custom_dir.mkdir()
    (custom_dir / "SKILL.md").write_text(
        "---\nname: My Custom\ndescription: test\n---\n\nbody",
        encoding="utf-8",
    )
    r = SkillRegistry(tmp_path)
    r.scan()
    assert "my-custom-skill" in r.skills


def test_scan_nonexistent_dir():
    """skills 目录不存在时 scan() 不崩溃，返回空。"""
    r = SkillRegistry("/nonexistent/path/skills")
    r.scan()
    assert len(r.skills) == 0


# ──────────────────────────────────────────────────────────────────────────
# list_for_agent 域级过滤
# ──────────────────────────────────────────────────────────────────────────

def test_list_for_agent_shared_visible_to_all(registry):
    """_shared 域 skill 对所有 agent 可见。"""
    for domain in ("manager", "video_production", "smart_query", "any_domain"):
        skills = registry.list_for_agent(domain)
        shared_ids = {s.id for s in skills if s.domain == "_shared"}
        assert {"dag-ops", "workflow-author"} <= shared_ids


def test_list_for_agent_domain_specific_filter(registry):
    """非 _shared 域 skill 只对匹配域的 agent 可见。"""
    # video_production 域 agent 能看到 video-edit
    video_skills = registry.list_for_agent("video_production")
    skill_ids = {s.id for s in video_skills}
    assert "video-edit" in skill_ids

    # manager 域 agent 看不到 video-edit
    manager_skills = registry.list_for_agent("manager")
    skill_ids = {s.id for s in manager_skills}
    assert "video-edit" not in skill_ids


def test_list_for_agent_sorted_by_id(registry):
    """返回结果按 id 排序。"""
    skills = registry.list_for_agent("manager")
    ids = [s.id for s in skills]
    assert ids == sorted(ids)


# ──────────────────────────────────────────────────────────────────────────
# get_skill_body
# ──────────────────────────────────────────────────────────────────────────

def test_get_skill_body_returns_content(registry):
    """get_skill_body 返回完整 body 文本。"""
    body = registry.get_skill_body("dag-ops")
    assert body is not None
    assert "# DAG 操作指南" in body


def test_get_skill_body_not_found(registry):
    """不存在的 skill_id 返回 None。"""
    body = registry.get_skill_body("nonexistent")
    assert body is None


# ──────────────────────────────────────────────────────────────────────────
# build_prompt_section
# ──────────────────────────────────────────────────────────────────────────

def test_build_prompt_section_contains_metadata(registry):
    """build_prompt_section 生成含 skill metadata 的 system_prompt 段。"""
    section = registry.build_prompt_section("manager")
    assert "## 可用 Skill" in section
    assert "dag-ops" in section
    assert "workflow-author" in section
    # 不含完整 body（节省 token）
    assert "触发工作流的方法" not in section


def test_build_prompt_section_excludes_other_domain(registry):
    """manager 域的 prompt 段不含 video_production 域 skill。"""
    section = registry.build_prompt_section("manager")
    assert "video-edit" not in section


def test_build_prompt_section_no_agent_domain(registry):
    """agent_domain=None 时返回全部 skill（调试用）。"""
    section = registry.build_prompt_section(None)
    assert "dag-ops" in section
    assert "video-edit" in section


def test_build_prompt_section_empty_registry(tmp_path):
    """空 registry 返回空字符串。"""
    r = SkillRegistry(tmp_path)
    r.scan()
    assert r.build_prompt_section("manager") == ""


def test_build_prompt_section_truncates_long_description(tmp_path):
    """description 超过 80 字符时截断。"""
    long_desc = "超长描述" * 30  # 120 字符
    skill_dir = tmp_path / "long-desc-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: long-desc\ndescription: {long_desc}\ndomain: _shared\n---\n\nbody",
        encoding="utf-8",
    )
    r = SkillRegistry(tmp_path)
    r.scan()
    section = r.build_prompt_section("manager")
    # 截断后含 "..."
    assert "..." in section


# ──────────────────────────────────────────────────────────────────────────
# read_skill 工具 handler
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_skill_handler_returns_body(registry, monkeypatch):
    """read_skill 工具正常返回 skill body。"""
    from orchestrator._registry import set_skill_registry
    set_skill_registry(registry)

    from tools.skill_tools import read_skill
    result = await read_skill({"skill_id": "dag-ops"})
    assert "content" in result
    assert "# DAG 操作指南" in result["content"]
    assert result["skill_id"] == "dag-ops"
    assert result["length"] > 0


@pytest.mark.asyncio
async def test_read_skill_handler_not_found(registry, monkeypatch):
    """read_skill 不存在的 skill 返回错误 + 可用列表。"""
    from orchestrator._registry import set_skill_registry
    set_skill_registry(registry)

    from tools.skill_tools import read_skill
    result = await read_skill({"skill_id": "nonexistent"})
    assert "error" in result
    assert result["error"] == "skill_not_found"
    assert "available" in result
    assert "dag-ops" in result["available"]


@pytest.mark.asyncio
async def test_read_skill_handler_missing_param():
    """read_skill 缺少 skill_id 参数返回错误。"""
    from tools.skill_tools import read_skill
    result = await read_skill({})
    assert "error" in result
    assert result["error"] == "missing_skill_id"


@pytest.mark.asyncio
async def test_read_skill_handler_registry_unavailable(monkeypatch):
    """SkillRegistry 未初始化时返回友好错误。"""
    from orchestrator._registry import set_skill_registry
    set_skill_registry(None)

    from tools.skill_tools import read_skill
    result = await read_skill({"skill_id": "dag-ops"})
    assert "error" in result
    assert result["error"] == "registry_unavailable"


# ──────────────────────────────────────────────────────────────────────────
# _parse_simple_yaml 边界情况
# ──────────────────────────────────────────────────────────────────────────

def test_parse_simple_yaml_scalar():
    """标量 key: value。"""
    result = _parse_simple_yaml("name: test\ndescription: hello")
    assert result["name"] == "test"
    assert result["description"] == "hello"


def test_parse_simple_yaml_inline_list():
    """inline list: [a, b, c]。"""
    result = _parse_simple_yaml("depends_on: [a, b, c]")
    assert result["depends_on"] == ["a", "b", "c"]


def test_parse_simple_yaml_block_list():
    """block list（换行后 - item）。"""
    result = _parse_simple_yaml("triggers:\n  - 创建\n  - 生成")
    assert result["triggers"] == ["创建", "生成"]


def test_parse_simple_yaml_quoted_string():
    """引号字符串去引号。"""
    result = _parse_simple_yaml('name: "quoted name"')
    assert result["name"] == "quoted name"


def test_parse_simple_yaml_empty_inline_list():
    """空 inline list: []。"""
    result = _parse_simple_yaml("depends_on: []")
    assert result["depends_on"] == []


def test_parse_simple_yaml_skip_comments():
    """注释行（# 开头）被跳过。"""
    result = _parse_simple_yaml("# comment\nname: test")
    assert result["name"] == "test"


def test_parse_simple_yaml_skip_empty_lines():
    """空行被跳过。"""
    result = _parse_simple_yaml("name: test\n\n\ndescription: hello")
    assert result["name"] == "test"
    assert result["description"] == "hello"


def test_strip_quotes_double():
    """双引号去引号。"""
    assert _strip_quotes('"hello"') == "hello"


def test_strip_quotes_single():
    """单引号去引号。"""
    assert _strip_quotes("'hello'") == "hello"


def test_strip_quotes_no_quotes():
    """无引号保持原样。"""
    assert _strip_quotes("hello") == "hello"


def test_strip_quotes_single_char():
    """单字符保持原样（不误判为引号）。"""
    assert _strip_quotes("a") == "a"


# ──────────────────────────────────────────────────────────────────────────
# SkillMeta dataclass
# ──────────────────────────────────────────────────────────────────────────

def test_skill_meta_post_init_infers_id_from_path():
    """SkillMeta id 为空时从 file_path 推断。"""
    meta = SkillMeta(
        id="",
        name="test",
        description="",
        domain="_shared",
        file_path="/skills/my-skill/SKILL.md",
    )
    assert meta.id == "my-skill"


def test_skill_meta_post_init_keeps_explicit_id():
    """SkillMeta 有显式 id 时不覆盖。"""
    meta = SkillMeta(
        id="explicit-id",
        name="test",
        description="",
        domain="_shared",
        file_path="/skills/my-skill/SKILL.md",
    )
    assert meta.id == "explicit-id"
