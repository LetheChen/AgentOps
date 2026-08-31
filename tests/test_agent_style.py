"""AgentStyleLoader 单测。

验证：
- 加载真实 config/agent_styles 下 3 个风格（critical/conservative/aggressive）
- get_overlay：critical 返回含"批判"的非空串；default/空串/nonexistent 返回空串
- get_style：critical 返回完整 dict
- list_styles：返回 3 个，按 name 排序
- 空目录场景：不报错，list_styles 返回空列表
- 临时目录 + 2 个 yaml：加载逻辑正确
"""
import os
import sys
import tempfile

import pytest
import pytest_asyncio
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from task.agent_style import AgentStyleLoader

# 项目根目录（tests/ 上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_STYLES_DIR = os.path.join(PROJECT_ROOT, "config", "agent_styles")


# ============================================================
# 真实 config/agent_styles 加载
# ============================================================

class TestLoadRealStyles:
    def test_loads_three_styles(self):
        """真实目录应加载 3 个风格。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        ids = {s["style_id"] for s in loader.list_styles()}
        assert ids == {"critical", "conservative", "aggressive"}

    def test_list_styles_sorted_by_name(self):
        """list_styles 按 name 排序：保守/批判/激进（中文按 codepoint）。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        styles = loader.list_styles()
        assert len(styles) == 3
        names = [s["name"] for s in styles]
        assert names == sorted(names)

    def test_get_style_critical_returns_full_dict(self):
        """get_style('critical') 返回完整 dict，含必填字段。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        s = loader.get_style("critical")
        assert s is not None
        assert s["style_id"] == "critical"
        assert s["name"] == "批判"
        assert "system_prompt_overlay" in s
        assert "permissions_overlay" in s
        assert "model_overlay" in s


# ============================================================
# get_overlay（异步）
# ============================================================

@pytest.mark.asyncio
class TestGetOverlay:
    async def test_critical_overlay_nonempty_and_contains_keyword(self):
        """critical overlay 非空且含"批判"。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        overlay = await loader.get_overlay("critical")
        assert overlay
        assert "批判" in overlay

    async def test_default_returns_empty(self):
        """default 返回空串。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        assert await loader.get_overlay("default") == ""

    async def test_empty_string_returns_empty(self):
        """空串返回空串。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        assert await loader.get_overlay("") == ""

    async def test_nonexistent_returns_empty(self):
        """不存在的 style_id 返回空串。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        assert await loader.get_overlay("nonexistent_style") == ""

    async def test_overlay_contains_style_section_header(self):
        """overlay 应含 === 风格覆盖 === 段落头（供 orchestrator 追加）。"""
        loader = AgentStyleLoader(REAL_STYLES_DIR)
        overlay = await loader.get_overlay("aggressive")
        assert "风格覆盖" in overlay


# ============================================================
# 空目录场景
# ============================================================

class TestEmptyDir:
    def test_nonexistent_dir_no_error(self):
        """目录不存在时不报错，list_styles 返回空列表。"""
        loader = AgentStyleLoader("/no/such/path/__not_exist__")
        assert loader.list_styles() == []

    def test_empty_temp_dir_no_error(self):
        """空临时目录不报错，list_styles 返回空列表。"""
        with tempfile.TemporaryDirectory() as d:
            loader = AgentStyleLoader(d)
            assert loader.list_styles() == []
            assert loader.get_style("anything") is None


# ============================================================
# 临时目录 + 2 个 yaml 文件
# ============================================================

class TestTempDirWithFiles:
    def test_loads_two_yaml_files(self):
        """临时目录放 2 个 yaml，应加载 2 个风格并按 name 排序。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "alpha.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({
                    "style_id": "alpha",
                    "name": "甲风格",
                    "description": "alpha desc",
                    "system_prompt_overlay": "=== 风格覆盖：甲 ===\n- alpha rule",
                    "permissions_overlay": {"denied_tools_add": ["bash"]},
                    "model_overlay": {},
                }, f, allow_unicode=True)
            with open(os.path.join(d, "beta.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({
                    "style_id": "beta",
                    "name": "乙风格",
                    "description": "beta desc",
                    "system_prompt_overlay": "=== 风格覆盖：乙 ===\n- beta rule",
                    "permissions_overlay": {},
                    "model_overlay": {},
                }, f, allow_unicode=True)

            loader = AgentStyleLoader(d)
            styles = loader.list_styles()
            assert len(styles) == 2
            ids = {s["style_id"] for s in styles}
            assert ids == {"alpha", "beta"}
            # 按 name 排序：乙(U+4E59) < 甲(U+7532)，所以乙风格在前
            names = [s["name"] for s in styles]
            assert names == sorted(names)
            assert names == ["乙风格", "甲风格"]

    @pytest.mark.asyncio
    async def test_temp_dir_get_overlay(self):
        """临时目录加载后 get_overlay 返回对应 overlay。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s1.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({
                    "style_id": "s1",
                    "name": "风格一",
                    "system_prompt_overlay": "=== 风格覆盖：一 ===\n- rule one",
                    "permissions_overlay": {},
                    "model_overlay": {},
                }, f, allow_unicode=True)
            loader = AgentStyleLoader(d)
            overlay = await loader.get_overlay("s1")
            assert "rule one" in overlay
            assert await loader.get_overlay("missing") == ""

    def test_skips_file_without_style_id(self):
        """缺少 style_id 的 yaml 应被跳过（不报错）。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "valid.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({
                    "style_id": "valid",
                    "name": "有效",
                    "system_prompt_overlay": "x",
                    "permissions_overlay": {},
                    "model_overlay": {},
                }, f, allow_unicode=True)
            with open(os.path.join(d, "invalid.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"name": "无 style_id"}, f, allow_unicode=True)
            loader = AgentStyleLoader(d)
            assert len(loader.list_styles()) == 1
            assert loader.get_style("valid") is not None
