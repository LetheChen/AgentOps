#!/usr/bin/env python3
"""
Video Production Skill - Duration & Audio Validation Script
校验音频时长、吞音、长空白、音频重复、场景-资源对齐等所有音频相关问题。

用法:
    python validate_duration.py <project_dir> [--target <seconds>] [--composition <html_file>]

示例:
    python validate_duration.py ./my-video --target 60 --composition code/my-composition.html
"""

import json
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ── ANSI colors ──────────────────────────────────────────────
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

# ── Constants ─────────────────────────────────────────────────
MIN_AUDIO_DURATION = 1.0       # 最小音频时长（秒），低于此值视为异常
MAX_GAP_BETWEEN_SCENES = 1.0   # 场景间最大允许静音间隔（秒）
TAIL_BUFFER_MIN = 0.3          # 最小尾部缓冲（秒）
TAIL_BUFFER_MAX = 0.5          # 最大尾部缓冲（秒）


# ═══════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class AudioInfo:
    """单个音频文件信息"""
    scene_id: str
    file_path: str
    file_exists: bool = False
    file_size: int = 0
    duration: float = 0.0
    errors: list = field(default_factory=list)


@dataclass
class SceneInfo:
    """场景信息（从 HTML 解析）"""
    scene_index: int
    data_duration: float = 0.0
    data_start: float = 0.0
    audio_src: str = ""
    errors: list = field(default_factory=list)


@dataclass
class ValidationReport:
    """校验报告"""
    project_dir: str
    total_audio_duration: float = 0.0
    audio_files: list = field(default_factory=list)        # AudioInfo list
    scenes: list = field(default_factory=list)              # SceneInfo list
    errors: list = field(default_factory=list)              # 阻断级错误
    warnings: list = field(default_factory=list)            # 警告
    passed: list = field(default_factory=list)              # 通过的检查项
    target_duration: Optional[float] = None
    image_count: int = 0
    audio_file_count: int = 0


# ═══════════════════════════════════════════════════════════════
# Section 1: Audio File Validation
# ═══════════════════════════════════════════════════════════════

def validate_audio_files(project_dir: Path, report: ValidationReport):
    """校验 audio/ 目录：文件存在性、非空、durations.json 一致性"""

    audio_dir = project_dir / "media" / "audio"
    durations_file = audio_dir / "durations.json"

    print(f"\n{BOLD}[1] 音频文件存在性与完整性检查{RESET}")

    # ── 1.1 durations.json 是否存在 ──
    if not durations_file.exists():
        report.errors.append("❌ 缺少 media/audio/durations.json —— 必须先运行 Step 3.1 的 ffprobe 测量命令生成此文件")
        _print_fail("durations.json 不存在")
        return
    _print_pass("durations.json 已找到")

    # ── 1.2 解析 durations.json ──
    try:
        with open(durations_file, 'r', encoding='utf-8') as f:
            durations_data = json.load(f)
    except json.JSONDecodeError as e:
        report.errors.append(f"❌ durations.json 格式错误: {e}")
        _print_fail("durations.json 不是有效的 JSON")
        return
    _print_pass("durations.json 格式正确")

    # ── 1.3 遍历音频条目 ──
    if isinstance(durations_data, list):
        entries = durations_data
    elif isinstance(durations_data, dict):
        # 支持嵌套格式: {"scenes": [{"id": "scene-01", "duration_s": 6.687, ...}, ...]}
        if "scenes" in durations_data and isinstance(durations_data["scenes"], list):
            entries = [
                {"scene": s.get("id", s.get("scene", "unknown")), "duration": s.get("duration_s", s.get("duration", 0))}
                for s in durations_data["scenes"]
            ]
        else:
            entries = [{"scene": k, "duration": v} for k, v in durations_data.items()]
    else:
        report.errors.append("❌ durations.json 内容格式无法识别（需为数组或对象）")
        _print_fail("durations.json 内容格式错误")
        return

    if not entries:
        report.errors.append("❌ durations.json 为空，无音频条目")
        _print_fail("durations.json 无内容")
        return

    _print_pass(f"durations.json 包含 {len(entries)} 条音频记录")

    # ── 1.4 逐条校验 ──
    for entry in entries:
        scene_id = entry.get("scene", "unknown")
        duration = entry.get("duration", 0)

        mp3_path = audio_dir / f"{scene_id}.mp3"
        ai = AudioInfo(scene_id=scene_id, file_path=str(mp3_path), duration=float(duration))

        # 文件存在
        if not mp3_path.exists():
            ai.errors.append(f"文件不存在: {mp3_path.name}")
            _print_fail(f"  {scene_id}.mp3 —— 文件不存在")
        else:
            ai.file_exists = True
            ai.file_size = mp3_path.stat().st_size
            if ai.file_size == 0:
                ai.errors.append(f"文件为空 (0 bytes): {mp3_path.name}")
                _print_fail(f"  {scene_id}.mp3 —— 文件为空 (0 bytes)")
            else:
                _print_pass(f"  {scene_id}.mp3 —— {duration:.2f}s, {ai.file_size:,} bytes")

        # 时长检查
        if duration <= 0:
            ai.errors.append(f"时长为 0 或负数: {duration}s")
            _print_fail(f"  {scene_id} —— 时长为 0 或负数")
        elif duration < MIN_AUDIO_DURATION:
            ai.errors.append(f"音频时长过短 ({duration:.2f}s < {MIN_AUDIO_DURATION}s)，可能生成失败或文本过短")
            _print_fail(f"  {scene_id} —— 音频过短: {duration:.2f}s")

        report.audio_files.append(ai)

    # ── 1.5 检查孤儿 mp3（不在 durations.json 中的 mp3 文件）──
    mp3_files = set(f.stem for f in audio_dir.glob("*.mp3") if f.name != "durations.json")
    json_scenes = set(ai.scene_id for ai in report.audio_files)
    orphan_mp3s = mp3_files - json_scenes
    if orphan_mp3s:
        for o in orphan_mp3s:
            report.warnings.append(f"⚠️  {o}.mp3 存在于 audio/ 目录但不在 durations.json 中（可能是多余的）")
            _print_warn(f"  {o}.mp3 —— 不在 durations.json 中")


# ═══════════════════════════════════════════════════════════════
# Section 2: Resource Mapping (1:1 对齐)
# ═══════════════════════════════════════════════════════════════

def validate_resource_mapping(project_dir: Path, report: ValidationReport):
    """校验场景-音频-图片的 1:1 对齐"""

    audio_dir = project_dir / "media" / "audio"
    image_dir = project_dir / "media" / "images"
    prompt_dir = project_dir / "media" / "prompts"

    print(f"\n{BOLD}[2] 场景-资源 1:1 对齐检查{RESET}")

    audio_count = len([f for f in audio_dir.glob("*.mp3") if f.name != "durations.json"])
    image_files = list(image_dir.glob("*.png")) + list(image_dir.glob("*.jpg")) + list(image_dir.glob("*.jpeg"))
    image_count = len(image_files)
    prompt_count = len(list(prompt_dir.glob("*.md"))) if prompt_dir.exists() else 0

    scene_ids = sorted(set(ai.scene_id for ai in report.audio_files))

    report.audio_file_count = audio_count
    report.image_count = image_count

    _print_pass(f"音频文件: {audio_count} 个")
    _print_pass(f"图片文件: {image_count} 个")
    _print_pass(f"Prompt 存档: {prompt_count} 个")

    # 场景数一致性
    if len(scene_ids) != audio_count:
        report.errors.append(
            f"❌ 场景数不匹配: durations.json 记录 {len(scene_ids)} 个场景，"
            f"但 audio/ 目录有 {audio_count} 个 mp3 文件"
        )
        _print_fail(f"场景数 ({len(scene_ids)}) != 音频文件数 ({audio_count})")
    else:
        _print_pass("场景数 == 音频文件数")

    # 图片数匹配
    if image_count != audio_count:
        report.warnings.append(
            f"⚠️  图片数 ({image_count}) != 音频数 ({audio_count}) —— "
            f"如果部分场景不需要图片可以忽略，否则请检查是否有遗漏"
        )
        _print_warn(f"图片数 ({image_count}) != 音频数 ({audio_count})")
    else:
        _print_pass("图片数 == 音频文件数")

    # 逐场景检查资源
    print(f"\n  {CYAN}逐场景资源对照:{RESET}")
    for sid in scene_ids:
        has_img = any(sid in f.stem for f in image_files)
        has_prompt = (prompt_dir / f"{sid}.md").exists()
        has_audio = (audio_dir / f"{sid}.mp3").exists()
        status = []
        if not has_img:
            status.append("缺图片")
        if not has_prompt:
            status.append("缺 prompt")
        if not has_audio:
            status.append("缺音频")
        if status:
            _print_fail(f"  {sid} —— 缺少: {', '.join(status)}")
        else:
            _print_pass(f"  {sid} —— 资源完整")


# ═══════════════════════════════════════════════════════════════
# Section 3: HTML Composition Validation
# ═══════════════════════════════════════════════════════════════

def validate_html_composition(html_path: Path, report: ValidationReport):
    """校验 HTML composition 中的音频引用、时间轴、吞音/长空白"""

    if not html_path or not html_path.exists():
        report.warnings.append(f"⚠️  未找到 HTML composition 文件: {html_path}")
        print(f"\n{BOLD}[3] HTML Composition 检查{RESET}")
        _print_warn("HTML 文件不存在，跳过此步")
        return

    print(f"\n{BOLD}[3] HTML Composition 音频时间轴检查{RESET}")
    print(f"  {CYAN}解析: {html_path}{RESET}")

    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # ── 3.1 提取所有 <audio> 标签 ──
    audio_srcs = re.findall(r'<audio[^>]*\ssrc=["\']([^"\']+)["\']', html)
    data_audio_attrs = re.findall(r'data-audio=["\']([^"\']+)["\']', html)

    all_audio_refs = audio_srcs + data_audio_attrs
    unique_audio_refs = set(all_audio_refs)

    _print_pass(f"共发现 {len(all_audio_refs)} 处音频引用, {len(unique_audio_refs)} 个唯一文件")

    # ── 3.2 音频唯一性校验 ──
    if len(all_audio_refs) != len(unique_audio_refs):
        from collections import Counter
        dupes = [src for src, count in Counter(all_audio_refs).items() if count > 1]
        for d in dupes:
            report.errors.append(f"❌ 音频重复引用: {d} 被引用了多次，每个场景必须有独立音频文件")
            _print_fail(f"音频重复: {d}")
    else:
        _print_pass("所有音频引用唯一，无重复")

    # ── 3.3 对比 HTML 引用 vs durations.json ──
    html_scene_ids = set()
    for ref in unique_audio_refs:
        stem = Path(ref).stem
        html_scene_ids.add(stem)

    json_scene_ids = set(ai.scene_id for ai in report.audio_files)

    missing_in_html = json_scene_ids - html_scene_ids
    missing_in_json = html_scene_ids - json_scene_ids

    if missing_in_html:
        report.errors.append(f"❌ durations.json 中的场景未在 HTML 中引用: {', '.join(sorted(missing_in_html))}")
        _print_fail(f"durations.json 有但 HTML 未引用: {missing_in_html}")
    if missing_in_json:
        report.warnings.append(f"⚠️  HTML 引用了不在 durations.json 中的音频: {', '.join(sorted(missing_in_json))}")
        _print_warn(f"HTML 有但 durations.json 无: {missing_in_json}")
    if not missing_in_html and not missing_in_json:
        _print_pass("HTML 音频引用与 durations.json 完全一致")

    # ── 3.4 解析场景的 data-duration 和 data-start ──
    # 匹配模式: data-duration="12.5" 或 data-duration="12"
    scene_blocks = re.findall(
        r'data-duration=["\']([\d.]+)["\'](?:[^>]*data-start=["\']([\d.]+)["\'])?',
        html
    )
    audio_in_block = re.findall(
        r'<(?:div|section)[^>]*data-audio=["\']([^"\']+)["\']',
        html
    )

    # 如果没有 data-duration，尝试从 data-start 推算
    data_durations = re.findall(r'data-duration=["\']([\d.]+)["\']', html)
    data_starts = re.findall(r'data-start=["\']([\d.]+)["\']', html)

    if not data_durations:
        report.errors.append("❌ HTML 中未找到任何 data-duration 属性 —— 无法校验时间轴")
        _print_fail("缺少 data-duration 属性")
        return

    _print_pass(f"解析到 {len(data_durations)} 个 data-duration")

    durations_list = [float(d) for d in data_durations]
    starts_list = [float(s) for s in data_starts] if data_starts else []

    # ── 3.5 吞音检测：data-duration >= 音频实测时长 ──
    print(f"\n  {CYAN}吞音检测（data-duration >= 音频实测时长）:{RESET}")
    audio_dur_map = {ai.scene_id: ai.duration for ai in report.audio_files}

    # 尝试从 HTML 中提取 data-duration 与场景的对应关系
    # 按出现顺序匹配
    scene_order = sorted(audio_dur_map.keys())

    for i, sid in enumerate(scene_order):
        actual_dur = audio_dur_map.get(sid, 0)
        html_dur = durations_list[i] if i < len(durations_list) else 0

        if html_dur <= 0:
            report.errors.append(f"❌ 场景 {sid}: data-duration 为 0 或缺失")
            _print_fail(f"  {sid}: data-duration 缺失或为 0")
            continue

        if html_dur < actual_dur:
            shortage = actual_dur - html_dur
            report.errors.append(
                f"❌ 吞音！场景 {sid}: data-duration={html_dur:.2f}s 小于音频实测 {actual_dur:.2f}s, "
                f"尾部将被截断 {shortage:.2f}s"
            )
            _print_fail(f"  吞音! {sid}: HTML {html_dur:.2f}s < 音频 {actual_dur:.2f}s (缺 {shortage:.2f}s)")
        elif html_dur < actual_dur + TAIL_BUFFER_MIN:
            report.warnings.append(
                f"⚠️  场景 {sid}: data-duration={html_dur:.2f}s, 缓冲仅 {html_dur - actual_dur:.2f}s "
                f"(建议 ≥ {TAIL_BUFFER_MIN}s)"
            )
            _print_warn(f"  {sid}: 缓冲不足 {html_dur - actual_dur:.2f}s (需 ≥{TAIL_BUFFER_MIN}s)")
        elif html_dur > actual_dur + TAIL_BUFFER_MAX:
            report.warnings.append(
                f"⚠️  场景 {sid}: data-duration={html_dur:.2f}s, 缓冲偏大 {html_dur - actual_dur:.2f}s "
                f"(建议 ≤ {TAIL_BUFFER_MAX}s)"
            )
            _print_warn(f"  {sid}: 缓冲偏大 {html_dur - actual_dur:.2f}s (建议 ≤{TAIL_BUFFER_MAX}s)")
        else:
            _print_pass(f"  {sid}: HTML {html_dur:.2f}s >= 音频 {actual_dur:.2f}s (缓冲 {html_dur - actual_dur:.2f}s)  ✓")

    # ── 3.6 长空白检测：相邻场景间纯静音 > 1s ──
    if len(starts_list) >= 2:
        print(f"\n  {CYAN}长空白检测（场景间间隙 > {MAX_GAP_BETWEEN_SCENES}s）:{RESET}")
        has_gap_issue = False
        for i in range(len(starts_list) - 1):
            current_end = starts_list[i] + (durations_list[i] if i < len(durations_list) else 0)
            next_start = starts_list[i + 1]
            gap = next_start - current_end

            scene_name = scene_order[i] if i < len(scene_order) else f"scene-{i+1:02d}"
            next_name = scene_order[i+1] if i+1 < len(scene_order) else f"scene-{i+2:02d}"

            if gap > MAX_GAP_BETWEEN_SCENES:
                report.errors.append(
                    f"❌ 长空白！{scene_name} → {next_name}: "
                    f"间隔 {gap:.2f}s (> {MAX_GAP_BETWEEN_SCENES}s)"
                )
                _print_fail(f"  长空白! {scene_name}→{next_name}: 间隔 {gap:.2f}s")
                has_gap_issue = True
            elif gap < -0.1:
                report.warnings.append(
                    f"⚠️  场景重叠！{scene_name} → {next_name}: "
                    f"重叠 {-gap:.2f}s"
                )
                _print_warn(f"  场景重叠! {scene_name}→{next_name}: 重叠 {-gap:.2f}s")
            else:
                _print_pass(f"  {scene_name}→{next_name}: 间隔 {gap:.2f}s  ✓")
        if not has_gap_issue:
            _print_pass("所有场景衔接紧密，无长空白")


# ═══════════════════════════════════════════════════════════════
# Section 4: Target Duration Check
# ═══════════════════════════════════════════════════════════════

def validate_target_duration(report: ValidationReport):
    """校验总时长是否符合目标"""

    total = sum(ai.duration for ai in report.audio_files if ai.duration > 0)

    # 加上缓冲：每个场景 +0.4s
    total_with_buffer = total + len(report.audio_files) * 0.4

    report.total_audio_duration = total

    print(f"\n{BOLD}[4] 目标时长校验{RESET}")
    print(f"  {CYAN}总音频实测时长: {total:.2f}s{ RESET}")
    print(f"  {CYAN}加缓冲后总时长: {total_with_buffer:.2f}s{ RESET}")

    # 输出场景-时间映射表
    print(f"\n  {BOLD}场景-时间映射表:{RESET}")
    print(f"  {'场景':<15} {'实测时长':<10} {'+缓冲0.4s':<12} {'累计':<10}")
    print(f"  {'-'*47}")
    cumulative = 0.0
    for ai in sorted(report.audio_files, key=lambda x: x.scene_id):
        scene_dur = ai.duration + 0.4
        cumulative += scene_dur
        print(f"  {ai.scene_id:<15} {ai.duration:<10.2f}s {scene_dur:<12.2f}s {cumulative:<10.2f}s")

    if report.target_duration:
        diff = total_with_buffer - report.target_duration
        if abs(diff) < 3:
            _print_pass(f"总时长 {total_with_buffer:.1f}s 匹配目标 {report.target_duration:.0f}s (偏差 {diff:+.1f}s)")
        elif diff > 0:
            report.warnings.append(
                f"⚠️  总时长 {total_with_buffer:.1f}s 超出目标 {report.target_duration:.0f}s (超出 {diff:.1f}s)"
            )
            _print_warn(f"超出目标 {diff:.1f}s —— 建议减少场景或缩短文案")
        else:
            report.warnings.append(
                f"⚠️  总时长 {total_with_buffer:.1f}s 不足目标 {report.target_duration:.0f}s (不足 {-diff:.1f}s)"
            )
            _print_warn(f"不足目标 {-diff:.1f}s —— 建议扩展描述或增加场景")


# ═══════════════════════════════════════════════════════════════
# Output Helpers
# ═══════════════════════════════════════════════════════════════

def _print_pass(msg: str):
    print(f"  [{GREEN}PASS{RESET}] {msg}")


def _print_fail(msg: str):
    print(f"  [{RED}FAIL{RESET}] {msg}")


def _print_warn(msg: str):
    print(f"  [{YELLOW}WARN{RESET}] {msg}")


def print_summary(report: ValidationReport):
    """打印最终摘要和修复建议"""

    print(f"\n{BOLD}{'='*60}")
    print(f"校验报告概要")
    print(f"{'='*60}{RESET}")

    error_count = len(report.errors)
    warn_count = len(report.warnings)

    if error_count == 0 and warn_count == 0:
        print(f"\n  {GREEN}{BOLD}✓ 全部校验通过！可以进入 Step 5 Hyperframes 合成。{RESET}")
        return

    # 先打印阻断级错误
    if error_count > 0:
        print(f"\n  {RED}{BOLD}⛔ 阻断级错误 ({error_count} 项) —— 必须修复后才能继续:{RESET}")
        for e in report.errors:
            print(f"    {e}")

    # 再打印警告
    if warn_count > 0:
        print(f"\n  {YELLOW}{BOLD}⚠️  警告 ({warn_count} 项) —— 建议修复:{RESET}")
        for w in report.warnings:
            print(f"    {w}")

    # 修复建议
    print(f"\n  {BOLD}常见修复方案:{RESET}")
    print(f"    吞音 → 修改 HTML 中该场景的 data-duration 为 音频实测时长 + 0.4s")
    print(f"    长空白 → 调整 data-start 使相邻场景间隔 ≤ 1.0s")
    print(f"    音频重复 → 为每个场景生成独立的 mp3 文件")
    print(f"    文件缺失 → 回 Step 3 重新生成对应资源")
    print(f"    时长偏差 → 回 Step 2 调整 narration.md 字数")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("用法: python validate_duration.py <project_dir> [--target <seconds>] [--composition <html_file>]")
        print("示例: python validate_duration.py ./my-video --target 60 --composition code/my-composition.html")
        sys.exit(1)

    project_dir = Path(sys.argv[1]).resolve()
    target_duration = None
    composition_path = None

    # 解析参数
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == '--target' and i + 1 < len(sys.argv):
            target_duration = float(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == '--composition' and i + 1 < len(sys.argv):
            composition_path = Path(sys.argv[i + 1])
            i += 2
        else:
            i += 1

    # 自动搜索 HTML composition
    if not composition_path:
        code_dir = project_dir / "code"
        if code_dir.exists():
            html_files = list(code_dir.glob("*.html"))
            if len(html_files) == 1:
                composition_path = html_files[0]
            elif len(html_files) > 1:
                print(f"{YELLOW}发现多个 HTML 文件，请用 --composition 指定:{RESET}")
                for hf in html_files:
                    print(f"  {hf}")
                composition_path = None  # 不自动选择，避免误判

    if not project_dir.exists():
        print(f"{RED}错误: 项目目录不存在: {project_dir}{RESET}")
        sys.exit(1)

    report = ValidationReport(
        project_dir=str(project_dir),
        target_duration=target_duration
    )

    print(f"{BOLD}{'='*60}")
    print(f"Video Production - 时长与音频校验")
    print(f"{'='*60}{RESET}")
    print(f"项目目录: {project_dir}")
    if target_duration:
        print(f"目标时长: {target_duration}s")
    if composition_path:
        print(f"HTML 文件: {composition_path}")
    print(f"{'='*60}")

    # 运行所有校验
    validate_audio_files(project_dir, report)
    validate_resource_mapping(project_dir, report)
    if composition_path:
        validate_html_composition(composition_path, report)
    else:
        print(f"\n{BOLD}[3] HTML Composition 检查{RESET}")
        _print_warn("未指定 HTML composition 文件，跳过时间轴吞音/长空白检查")

    validate_target_duration(report)

    # 打印摘要
    print_summary(report)

    # 返回码：有阻断错误 → 1；仅有警告 → 0
    return 1 if report.errors else 0


if __name__ == '__main__':
    sys.exit(main())
