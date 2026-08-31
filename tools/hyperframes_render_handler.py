"""hyperframes_render 工具的 Python handler —— 确定性渲染。

从 compose 阶段的素材（storyboard、durations.json、图片、音频）生成
合规 hyperframes composition 并渲染为 MP4。不依赖 LLM 生成 HTML 质量。
"""
import json
import os
import re
import shutil
import subprocess


# ── hyperframes composition 模板 ──
COMPOSITION_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a0f; overflow: hidden; font-family: 'Inter', system-ui, -apple-system, sans-serif; }}

  [data-composition-id="video-main"] {{
    width: 100%; height: 100%;
    position: relative;
    letter-spacing: -0.02em;
  }}

  .bg-image {{
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    object-fit: cover;
  }}

  .overlay {{
    position: absolute; inset: 0; z-index: 1;
    background: linear-gradient(to top, rgba(10,10,15,0.92) 0%, rgba(10,10,15,0.35) 45%, transparent 75%);
  }}

  .scene-content {{
    position: absolute;
    bottom: 0; left: 0; right: 0;
    z-index: 2;
    display: flex;
    flex-direction: column;
    padding: 80px 100px 64px;
    gap: 14px;
  }}

  .tag-line {{
    display: inline-flex; align-items: center; gap: 8px;
    background: rgba(255,255,255,0.07);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 100px;
    padding: 5px 16px;
    font-size: 15px; font-weight: 600;
    color: #a78bfa;
    align-self: flex-start;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }}

  .headline {{
    font-size: 48px; font-weight: 700;
    color: #ffffff;
    line-height: 1.15;
    letter-spacing: -0.03em;
    max-width: 820px;
  }}
  .headline .accent {{ color: #7c3aed; }}

  .subtitle {{
    font-size: 22px; font-weight: 400;
    color: rgba(255,255,255,0.70);
    line-height: 1.5;
    max-width: 680px;
  }}

  .feature-row {{
    display: flex; gap: 14px; margin-top: 4px;
  }}

  .feature-card {{
    display: flex; flex-direction: column; gap: 4px;
    background: rgba(255,255,255,0.05);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    padding: 16px 20px;
    flex: 1; min-width: 0;
  }}
  .feature-card .label {{
    font-size: 12px; font-weight: 600;
    color: rgba(255,255,255,0.40);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}
  .feature-card .value {{
    font-size: 26px; font-weight: 700;
    color: #ffffff;
  }}
  .feature-card .hint {{
    font-size: 13px; font-weight: 400;
    color: rgba(255,255,255,0.45);
    line-height: 1.35;
  }}
</style>
</head>
<body>
<div
  data-composition-id="video-main"
  data-width="1920"
  data-height="1080"
  data-start="0"
>
  {scenes_html}
</div>

<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.7/dist/gsap.min.js"></script>
<script>
  window.__timelines = window.__timelines || {{}};
  var tl = gsap.timeline({{ paused: true }});
  window.__timelines["video-main"] = tl;

  // 每场景: 背景放大淡入 + 文字元素入场
  var sceneEls = document.querySelectorAll('[data-scene]');
  sceneEls.forEach(function(scene) {{
    var sidx = parseInt(scene.getAttribute('data-scene'));
    var sStart = parseFloat(scene.getAttribute('data-start'));

    // 背景缓慢放大
    var bgImg = scene.querySelector('.bg-image');
    if (bgImg) {{
      var bgStart = parseFloat(bgImg.getAttribute('data-start') || sStart);
      tl.from(bgImg, {{
        scale: 1.06, duration: 2.0, ease: 'power2.out'
      }}, bgStart);
    }}

    // 文字依次入场
    var stepEls = scene.querySelectorAll('.step-content');
    stepEls.forEach(function(el, i) {{
      var stepStart = parseFloat(el.getAttribute('data-start') || sStart);
      var tag = el.querySelector('.tag-line');
      var hd = el.querySelector('.headline');
      var sub = el.querySelector('.subtitle');
      var cards = el.querySelectorAll('.feature-card');

      if (tag) tl.from(tag, {{ y: 30, opacity: 0, duration: 0.45, ease: 'power3.out' }}, stepStart + 0.05);
      if (hd)  tl.from(hd,  {{ y: 40, opacity: 0, duration: 0.60, ease: 'expo.out' }},  stepStart + 0.15);
      if (sub) tl.from(sub, {{ y: 30, opacity: 0, duration: 0.45, ease: 'power2.out' }}, stepStart + 0.30);
      cards.forEach(function(c, ci) {{
        tl.from(c, {{ y: 35, opacity: 0, scale: 0.92, duration: 0.45, ease: 'back.out(1.3)' }}, stepStart + 0.40 + ci * 0.12);
      }});
    }});
  }});

  // 结尾淡出
  tl.to('[data-composition-id="video-main"]', {{ opacity: 0, duration: 0.8 }}, {total_duration} - 0.8);
</script>
</body>
</html>
"""

SCENE_HTML = """  <!-- ====== Scene {scene_index} ({duration_s}s) ====== -->
  <img
    id="s{scene_index}-bg"
    class="clip bg-image"
    data-start="{scene_start}"
    data-duration="{duration_s}"
    data-track-index="{bg_track}"
    src="media/images/scene-{scene_index:02d}.png"
    alt=""
  >
  <div
    id="s{scene_index}-overlay"
    class="clip overlay"
    data-start="{scene_start}"
    data-duration="{duration_s}"
    data-track-index="{overlay_track}"
  ></div>
  <div
    id="s{scene_index}-content"
    class="clip scene-content"
    data-start="{scene_start}"
    data-duration="{duration_s}"
    data-track-index="{content_track}"
    data-scene="{scene_index}"
  >
    <div class="step-content" data-start="{scene_start}" data-duration="{duration_s}">
      <div class="tag-line">{tag_text}</div>
      <div class="headline">{headline_html}</div>
      {cards_html}
    </div>
  </div>
  <audio
    id="s{scene_index}-audio"
    data-start="{scene_start}"
    data-duration="{duration_s}"
    data-track-index="{audio_track}"
    data-volume="1"
    src="media/audio/scene-{scene_index:02d}.mp3"
  ></audio>
"""


def _load_durations(workspace_root: str) -> dict | None:
    """加载 durations.json。"""
    path = os.path.join(workspace_root, "media", "audio", "durations.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _parse_storyboard(workspace_root: str) -> list[dict]:
    """从 storyboard.md 提取场景信息。

    返回：[{index, title, tag, description, cards: [{label, value, hint}]}]
    无 storyboard 时返回空列表。
    """
    path = os.path.join(workspace_root, "script", "storyboard.md")
    if not os.path.isfile(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    scenes = []
    # 匹配 "## Scene NN — 标题"
    scene_blocks = re.split(r'\n## Scene (\d+)\s', text)
    # scene_blocks[0] = preamble, then pairs of [index, content]
    for i in range(1, len(scene_blocks), 2):
        try:
            idx = int(scene_blocks[i])
        except ValueError:
            continue
        content = scene_blocks[i + 1] if i + 1 < len(scene_blocks) else ""

        # 提取标题（"— 后面的部分到换行"）
        title_m = re.search(r'[—\-\u2014\u2013]\s*(.+?)(?:\n|$)', content)
        title = title_m.group(1).strip() if title_m else f"场景 {idx}"

        # 提取第一条口播作 tag
        narration_m = re.search(r'>\s*(.+?)(?:\n|$)', content)
        tag = narration_m.group(1).strip()[:40] if narration_m else title

        # 提取分镜描述第一行作 subtitle
        desc_lines = [l.strip() for l in content.split('\n') if l.strip() and not l.startswith('#') and not l.startswith('>') and not l.startswith('|') and not l.startswith('-') and not l.startswith('*')]
        description = ""
        for line in desc_lines:
            if len(line) > 10 and not line.startswith('`'):
                description = line[:120]
                break

        scenes.append({
            "index": idx,
            "title": title,
            "tag": tag,
            "description": description,
            "cards": [],  # 暂不自动提取卡片
        })

    return scenes


def _generate_composition(workspace_root: str) -> str:
    """从素材生成 hyperframes composition HTML。"""
    dur = _load_durations(workspace_root)
    storyboard = _parse_storyboard(workspace_root)

    if not dur or "scenes" not in dur:
        # 没有 durations.json：无法确定时间轴，退回到 agent 的 HTML
        html_path = os.path.join(workspace_root, "code", "composition.html")
        if os.path.isfile(html_path):
            return _wrap_agent_html(html_path)
        raise RuntimeError("无 durations.json 且无 composition.html，无法生成时间轴")

    scenes_data = dur["scenes"]
    total_duration = dur.get("actual_total_duration_s", 0)

    scenes_html_parts = []
    current_time = 0.0
    for i, sd in enumerate(scenes_data):
        idx = sd.get("index", i + 1)
        dur_s = sd.get("duration_s", 10)
        scene_start = current_time

        # 从 storyboard 匹配标题
        sb = next((s for s in storyboard if s["index"] == idx), None)
        tag_text = sb["tag"] if sb else f"场景 {idx}"
        headline_html = sb["title"] if sb else ""

        # 生成卡片（如果有）
        cards_html = ""
        if sb and sb.get("cards"):
            cards = sb["cards"]
            cards_html = '<div class="feature-row">\n'
            for c in cards:
                cards_html += (
                    f'      <div class="feature-card">\n'
                    f'        <span class="label">{c.get("label", "")}</span>\n'
                    f'        <span class="value">{c.get("value", "")}</span>\n'
                    f'        <span class="hint">{c.get("hint", "")}</span>\n'
                    f'      </div>\n'
                )
            cards_html += '    </div>'

        # 检查素材文件是否存在
        img_file = os.path.join(workspace_root, "media", "images", f"scene-{idx:02d}.png")
        audio_file = os.path.join(workspace_root, "media", "audio", f"scene-{idx:02d}.mp3")

        if not os.path.isfile(img_file):
            # 图片缺失：生成纯色占位图
            _generate_placeholder_image(workspace_root, idx, img_file)
        if not os.path.isfile(audio_file):
            continue

        scene_html = SCENE_HTML.format(
            scene_index=idx,
            duration_s=dur_s,
            scene_start=scene_start,
            bg_track=i * 4,
            overlay_track=i * 4 + 1,
            content_track=i * 4 + 2,
            audio_track=20 + i,
            tag_text=tag_text,
            headline_html=headline_html or f'场景 {idx}',
            cards_html=cards_html,
        )
        scenes_html_parts.append(scene_html)
        current_time += dur_s

    if not scenes_html_parts:
        raise RuntimeError("无有效场景数据（检查 media/images/ 和 media/audio/ 文件）")

    return COMPOSITION_TEMPLATE.format(
        scenes_html="\n".join(scenes_html_parts),
        total_duration=total_duration or current_time,
    )


def _wrap_agent_html(html_path: str) -> str:
    """兜底：简单包装 agent 生成的 HTML（尽力而为）。"""
    with open(html_path, "r", encoding="utf-8") as f:
        raw = f.read()

    root_id = "hf-root"
    if 'data-composition-id=' not in raw:
        # 找 body 后最大的 div 容器
        m = re.search(r'<body[^>]*>(.*?)</body>', raw, re.DOTALL)
        if m:
            body_content = m.group(1)
            # 跳过 driver/comment 类 div，找实际容器
            divs = list(re.finditer(r'<div\b([^>]*class="([^"]*)"[^>]*)>', body_content))
            target_div = None
            for dm in divs:
                cls = dm.group(2)
                if any(k in cls.lower() for k in ('stage', 'scene', 'main', 'container', 'wrapper', 'content', 'root')):
                    target_div = dm
                    break
            if not target_div and divs:
                target_div = divs[0]  # 兜底：用第一个 div

            if target_div:
                new_attrs = (f'{target_div.group(1)} data-composition-id="{root_id}" '
                             f'data-width="1920" data-height="1080" data-start="0"')
                raw = raw[:m.start(1) + target_div.start(1)] + new_attrs + raw[m.start(1) + target_div.end(1):]

    if 'window.__timelines' not in raw:
        timeline = f'''
<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.7/dist/gsap.min.js"></script>
<script>
window.__timelines = window.__timelines || {{}};
(function() {{
  var tl = gsap.timeline({{ paused: true }});
  window.__timelines["{root_id}"] = tl;
  var clips = document.querySelectorAll("[data-composition-id='{root_id}'] .clip");
  clips.forEach(function(el, i) {{
    tl.from(el, {{ opacity: 0, duration: 0.5, ease: "power2.out" }}, parseFloat(el.getAttribute("data-start")||"0") + 0.1);
  }});
}})();
</script>
'''
        raw = raw.replace('</body>', timeline + '\n</body>')

    return raw


def _copy_assets(workspace_root: str, project_dir: str):
    """复制 media/ 下的音频和图片到项目目录。"""
    for sub in ("audio", "images"):
        src = os.path.join(workspace_root, "media", sub)
        dst = os.path.join(project_dir, "media", sub)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)


def _generate_placeholder_image(workspace_root: str, scene_idx: int, output_path: str):
    """生成纯色渐变占位图（480p）。无外部依赖。"""
    width, height = 1920, 1080
    # 用 scene index 选一种颜色主题
    colors = [
        (10, 10, 20, 30, 20, 60),     # 深紫
        (10, 20, 30, 20, 40, 80),     # 深蓝
        (20, 10, 20, 60, 20, 80),     # 深绿
        (30, 20, 10, 80, 50, 20),     # 深橙
        (10, 15, 25, 50, 30, 100),    # 靛蓝
    ]
    c = colors[(scene_idx - 1) % len(colors)]
    r1, g1, b1, r2, g2, b2 = c

    # 用纯 RGB 字节生成最简单的渐变色块
    import struct
    def _write_ppm():
        with open(output_path + ".ppm", "wb") as f:
            f.write(f"P6\n{width} {height}\n255\n".encode())
            for y in range(height):
                t = y / height
                rr = int(r1 + (r2 - r1) * t)
                gg = int(g1 + (g2 - g1) * t)
                bb = int(b1 + (b2 - b1) * t)
                row = struct.pack('BBB' * width, *[rr, gg, bb] * width)
                f.write(row)

    try:
        _write_ppm()
        # 转 PNG（如果 ffmpeg 可用）
        subprocess.run(
            ["ffmpeg", "-y", "-i", output_path + ".ppm", output_path],
            capture_output=True, timeout=30,
        )
        os.remove(output_path + ".ppm")
    except Exception:
        # ffmpeg 不可用：保留 PPM，改后缀为 png（hyperframes 能读）
        if os.path.isfile(output_path + ".ppm"):
            os.replace(output_path + ".ppm", output_path.replace(".png", ".ppm"))
            # 复制一份
            with open(output_path, "w") as f:
                f.write("placeholder")


def run(html: str = "", output: str = "", **kwargs) -> dict:
    """hyperframes_render 工具入口。

    参数由 LLM 传入：html=composition.html 路径, output=mp4 目标路径。
    优先从素材（durations.json + 图片 + 音频）生成 composition；
    agent 的 composition.html 作为兜底。
    """
    # workspace root 从 html 路径推算（workspace/video-pipeline/run_xxx/code/composition.html）
    workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(html)))

    # 项目根（AgentOps/）从 workspace_root 往上直到找到 web/ 目录
    proot = os.path.abspath(workspace_root)
    for _ in range(10):
        if os.path.isdir(os.path.join(proot, "web")):
            break
        proot = os.path.dirname(proot)
    web_dir = os.path.join(proot, "web")

    # 确保输出目录存在
    out_dir = os.path.dirname(output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # 创建 project dir
    project_dir = os.path.join(workspace_root, ".hyperframes_project")
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir)
    os.makedirs(project_dir, exist_ok=True)

    # Step 1: 生成 / 包装 composition
    try:
        composition_html = _generate_composition(workspace_root)
    except RuntimeError as e:
        return {"error": f"生成 composition 失败: {e}", "is_error": True}

    index_path = os.path.join(project_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(composition_html)

    # Step 2: 复制素材
    _copy_assets(workspace_root, project_dir)

    # Step 3: 渲染
    cmd = (
        f'npx --prefix "{web_dir}" hyperframes render '
        f'-o "{output}" '
        f'--quiet '
        f'"{project_dir}"'
    )

    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=600, encoding="utf-8", errors="ignore",
            cwd=project_dir,
        )
    except subprocess.TimeoutExpired:
        return {"error": "hyperframes 渲染超时（10分钟）", "is_error": True}

    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[-600:]
        return {"error": f"hyperframes 渲染失败 (rc={r.returncode}): {err}", "is_error": True}

    if not os.path.isfile(output):
        return {"error": f"渲染完成但未找到输出文件: {output}", "is_error": True}

    return {
        "content": f"视频渲染成功，输出: {output}，大小: {os.path.getsize(output)} bytes",
        "output": output,
        "size_bytes": os.path.getsize(output),
    }
