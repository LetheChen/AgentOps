#!/usr/bin/env python
"""video_compose.py — 从素材生成 hyperframes composition + 渲染 MP4

用法: python video_compose.py --workspace <path> --output <mp4> --target-duration <sec>
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


COMPOSITION_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a0f; overflow: hidden; font-family: 'Inter', system-ui, -apple-system, sans-serif; }}
  [data-composition-id="video-main"] {{
    width: 100%; height: 100%; position: relative;
    letter-spacing: -0.02em;
  }}
  .bg-image {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
  .overlay {{
    position: absolute; inset: 0; z-index: 1;
    background: linear-gradient(to top, rgba(10,10,15,0.92) 0%, rgba(10,10,15,0.35) 45%, transparent 75%);
  }}
  .scene-content {{
    position: absolute; bottom: 0; left: 0; right: 0; z-index: 2;
    display: flex; flex-direction: column; padding: 80px 100px 64px; gap: 14px;
  }}
  .tag-line {{
    display: inline-flex; align-items: center; gap: 8px;
    background: rgba(255,255,255,0.07);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 100px; padding: 5px 16px;
    font-size: 15px; font-weight: 600; color: #a78bfa;
    align-self: flex-start; letter-spacing: 0.05em; text-transform: uppercase;
  }}
  .headline {{
    font-size: 48px; font-weight: 700; color: #fff;
    line-height: 1.15; letter-spacing: -0.03em; max-width: 820px;
  }}
  .subtitle {{
    font-size: 22px; font-weight: 400; color: rgba(255,255,255,0.70);
    line-height: 1.5; max-width: 680px;
  }}
</style>
</head>
<body>
<div data-composition-id="video-main" data-width="1920" data-height="1080" data-start="0">
  {scenes_html}
</div>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.7/dist/gsap.min.js"></script>
<script>
  window.__timelines = window.__timelines || {{}};
  var tl = gsap.timeline({{ paused: true }});
  window.__timelines["video-main"] = tl;
  var sceneEls = document.querySelectorAll('[data-scene]');
  sceneEls.forEach(function(scene) {{
    var sStart = parseFloat(scene.getAttribute('data-start'));
    var bgImg = scene.querySelector('.bg-image');
    if (bgImg) tl.from(bgImg, {{ scale: 1.06, duration: 2.0, ease: 'power2.out' }}, sStart);
    var tag = scene.querySelector('.tag-line');
    var hd = scene.querySelector('.headline');
    if (tag) tl.from(tag, {{ y: 30, opacity: 0, duration: 0.45, ease: 'power3.out' }}, sStart + 0.05);
    if (hd)  tl.from(hd,  {{ y: 40, opacity: 0, duration: 0.60, ease: 'expo.out'  }}, sStart + 0.15);
  }});
  tl.to('[data-composition-id="video-main"]', {{ opacity: 0, duration: 0.8 }}, {total_duration} - 0.8);
</script>
</body>
</html>
"""

SCENE_HTML = """  <img class="clip bg-image" data-start="{scene_start}" data-duration="{duration_s}" data-track-index="{bg_track}" src="media/images/scene-{scene_index:02d}.png" alt="">
  <div class="clip overlay" data-start="{scene_start}" data-duration="{duration_s}" data-track-index="{overlay_track}"></div>
  <div class="clip scene-content" data-start="{scene_start}" data-duration="{duration_s}" data-track-index="{content_track}" data-scene="{scene_index}">
    <div class="tag-line">{tag_text}</div>
    <div class="headline">{headline}</div>
  </div>
  <audio data-start="{scene_start}" data-duration="{duration_s}" data-track-index="{audio_track}" data-volume="1" src="media/audio/scene-{scene_index:02d}.mp3"></audio>
"""


def load_durations(ws_root: Path) -> dict | None:
    p = ws_root / "media" / "audio" / "durations.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8-sig"))


def parse_storyboard(ws_root: Path) -> dict[int, dict]:
    """返回 {scene_index: {title, tag, headline}}"""
    p = ws_root / "script" / "storyboard.md"
    if not p.is_file():
        return {}
    text = p.read_text(encoding="utf-8")
    scenes = {}
    blocks = text.split("## Scene ")
    for block in blocks[1:]:
        try:
            first_line = block.split("\n", 1)[0]
            idx = int(first_line.split()[0])
            title = " ".join(first_line.split()[1:]).lstrip(r"—\-–")
            # 第一句引用作 tag
            lines = block.split("\n")
            tag = f"Scene {idx}"
            for line in lines:
                line = line.strip()
                if line.startswith(">"):
                    tag = line.lstrip("> ").rstrip("。，,.;；")[:30]
                    break
            scenes[idx] = {"title": title.strip() or f"场景 {idx}", "tag": tag, "headline": title.strip() or f"场景 {idx}"}
        except (ValueError, IndexError):
            continue
    return scenes


def generate_composition(ws_root: Path, target_duration: int) -> str:
    dur = load_durations(ws_root)
    sb = parse_storyboard(ws_root)

    if not dur or "scenes" not in dur:
        raise RuntimeError("missing media/audio/durations.json or scenes field")

    scenes_data = dur["scenes"]
    total_duration = max(target_duration, dur.get("actual_total_duration_s", 0))

    scenes_html_parts = []
    current_time = 0.0
    for i, sd in enumerate(scenes_data):
        idx = sd.get("scene", i + 1)
        if idx is None:
            idx = i + 1
        # 兼容 "scene" 字段
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = i + 1
        dur_s = sd.get("duration_s", 5.0)
        scene_start = current_time

        img_file = ws_root / "media" / "images" / f"scene-{idx:02d}.png"
        audio_file = ws_root / "media" / "audio" / f"scene-{idx:02d}.mp3"

        # 缺图：生成纯色占位
        if not img_file.is_file():
            generate_placeholder(ws_root, idx, img_file)

        if not audio_file.is_file():
            continue

        sb_info = sb.get(idx, {})
        scenes_html_parts.append(SCENE_HTML.format(
            scene_index=idx,
            duration_s=dur_s,
            scene_start=scene_start,
            bg_track=i * 4,
            overlay_track=i * 4 + 1,
            content_track=i * 4 + 2,
            audio_track=20 + i,
            tag_text=sb_info.get("tag", f"场景 {idx}"),
            headline=sb_info.get("headline", f"场景 {idx}"),
        ))
        current_time += dur_s

    if not scenes_html_parts:
        raise RuntimeError("no valid scenes (check media/images/ and media/audio/)")

    return COMPOSITION_TEMPLATE.format(
        scenes_html="\n".join(scenes_html_parts),
        total_duration=total_duration,
    )


def generate_placeholder(ws_root: Path, scene_idx: int, output_path: Path):
    """生成纯色占位 PNG（用 ffmpeg）"""
    if output_path.is_file():
        return
    colors = [
        "0x1a1a3e",  # 深紫
        "0x1e3a5f",  # 深蓝
        "0x3e1a4f",  # 深绿紫
        "0x5f3a1e",  # 深橙
        "0x2a1f5f",  # 靛蓝
    ]
    color = colors[(scene_idx - 1) % len(colors)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1920x1080:d=1",
         "-frames:v", "1", str(output_path)],
        capture_output=True, timeout=30,
    )


def copy_assets(ws_root: Path, project_dir: Path):
    for sub in ("audio", "images"):
        src = ws_root / "media" / sub
        dst = project_dir / "media" / sub
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)


def find_web_dir() -> Path:
    """从当前位置往上找到包含 web/ 目录的祖先"""
    p = Path.cwd().resolve()
    for _ in range(10):
        if (p / "web").is_dir():
            return p / "web"
        p = p.parent
    raise RuntimeError("web/ directory not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--target-duration", type=int, default=60)
    args = ap.parse_args()

    ws_root = Path(args.workspace).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    # 1. 生成 composition
    html = generate_composition(ws_root, args.target_duration)

    # 2. 项目目录
    project_dir = ws_root / ".hyperframes_project"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    index_path = project_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    # 3. 复制素材
    copy_assets(ws_root, project_dir)

    # 4. 渲染
    web_dir = find_web_dir()
    cmd = f'npx --prefix "{web_dir}" hyperframes render -o "{output}" --quiet "{project_dir}"'
    print(f">>> render cmd: {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="ignore")

    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[-800:]
        print(f"✗ hyperframes render 失败 (rc={r.returncode}): {err}", file=sys.stderr)
        sys.exit(1)

    if not output.is_file():
        print(f"✗ 输出文件不存在: {output}", file=sys.stderr)
        sys.exit(1)

    size = output.stat().st_size
    print(f"✓ 视频生成成功: {output} ({size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()