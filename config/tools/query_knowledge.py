#!/usr/bin/env python3
"""视频生产知识库查询 tool（混合方案核心组件）

agent 执行过程中按需查询 references 和 prompt-templates，
避免 system_prompt 全量注入（~6k tokens → ~500 tokens system_prompt + 按需查询）。

用法:
    python query_knowledge.py --category narration_style
    python query_knowledge.py --category narration_style --section "念出来测试"
    python query_knowledge.py --category image_prompts --scene_type kpi_dashboard

输出: JSON 格式 {category, section, scene_type, rules, templates}
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

# 知识库根目录（相对项目根）
KB_ROOT = Path(__file__).resolve().parent.parent / "knowledge" / "video-production"

# 类别 → 文件映射
CATEGORY_MAP = {
    "narration_style": "NARRATION-STYLE.md",
    "storyboard_format": "STORYBOARD-FORMAT.md",
    "image_prompts": "IMAGE-PROMPTS.md",
    "composition_design": "COMPOSITION-DESIGN.md",
}

# scene_type → IMAGE-PROMPTS.md 中的章节关键词
SCENE_TYPE_MAP = {
    "poster": "开场/封面",
    "kpi_dashboard": "数据展示",
    "comparison": "对比/测评",
    "tutorial": "步骤讲解",
    "product": "产品展示",
    "brand_poster": "品牌主视觉",
    "infographic": "信息图",
    "map": "城市/地域",
}


def extract_section(content: str, section_keyword: str) -> str:
    """从 markdown 内容中提取包含关键词的章节（### 或 ## 级别）"""
    lines = content.split("\n")
    start = None
    end = None
    for i, line in enumerate(lines):
        if section_keyword.lower() in line.lower() and line.startswith("#"):
            start = i
        elif start is not None and line.startswith("# ") and i > start:
            # 遇到下一个一级标题
            end = i
            break
    if start is not None:
        return "\n".join(lines[start : end or len(lines)])
    return content


def extract_scene_template(content: str, scene_keyword: str) -> str:
    """从 IMAGE-PROMPTS.md 中提取匹配场景类型的模板章节"""
    lines = content.split("\n")
    start = None
    end = None
    for i, line in enumerate(lines):
        if scene_keyword.lower() in line.lower() and line.startswith("###"):
            start = max(0, i)
        elif start is not None and line.startswith("### ") and i > start + 2:
            end = i
            break
    if start is not None:
        return "\n".join(lines[start : end or len(lines)])
    # 如果没匹配到具体场景，返回通用模板
    return extract_section(content, "通用 Prompt 结构模板")


def query_prompt_templates(scene_type: str) -> list[dict]:
    """从 prompt-templates/ 目录查询匹配的 JSON 样本"""
    template_dir = KB_ROOT / "prompt-templates"
    if not template_dir.exists():
        return []

    # scene_type 关键词
    keywords = []
    if scene_type in SCENE_TYPE_MAP:
        keywords.append(SCENE_TYPE_MAP[scene_type])
    keywords.append(scene_type.lower())

    results = []
    for f in glob.glob(str(template_dir / "*.json")):
        try:
            data = json.load(open(f, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        # 按 category/tags/title 匹配
        matched = False
        category = data.get("category", "").lower()
        tags = [t.lower() for t in data.get("tags", [])]
        title = data.get("title", "").lower()

        for kw in keywords:
            if kw in category or kw in title or any(kw in t for t in tags):
                matched = True
                break

        # 如果没匹配关键词，返回所有样本（让 agent 自己选）
        if matched or not scene_type:
            results.append({
                "id": data.get("id"),
                "title": data.get("title"),
                "summary": data.get("summary"),
                "category": data.get("category"),
                "tags": data.get("tags"),
                "prompt": data.get("prompt"),
                "preview": data.get("previewImageUrl"),
                "source": data.get("source"),
            })

    return results


def query_knowledge(category: str, section: str = None, scene_type: str = None) -> dict:
    """主查询函数"""
    filename = CATEGORY_MAP.get(category)
    if not filename:
        return {
            "error": f"Unknown category: {category}",
            "available": list(CATEGORY_MAP.keys()),
        }

    filepath = KB_ROOT / filename
    if not filepath.exists():
        return {"error": f"Knowledge file not found: {filepath}"}

    content = filepath.read_text(encoding="utf-8")

    # 提取章节
    if section:
        rules = extract_section(content, section)
    elif category == "image_prompts" and scene_type:
        # image_prompts 按场景类型查模板
        scene_keyword = SCENE_TYPE_MAP.get(scene_type, scene_type)
        rules = extract_scene_template(content, scene_keyword)
    else:
        rules = content

    # image_prompts 同时查 prompt-templates 样本
    templates = None
    if category == "image_prompts":
        templates = query_prompt_templates(scene_type or "")

    return {
        "category": category,
        "section": section,
        "scene_type": scene_type,
        "rules": rules,
        "templates": templates,
    }


def main():
    parser = argparse.ArgumentParser(description="视频生产知识库查询")
    parser.add_argument("--category", required=True,
                        choices=list(CATEGORY_MAP.keys()),
                        help="知识类别")
    parser.add_argument("--section", default=None,
                        help="章节关键词（可选）")
    parser.add_argument("--scene-type", default=None,
                        help="场景类型（image_prompts 专用）")
    args = parser.parse_args()

    result = query_knowledge(args.category, args.section, args.scene_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
