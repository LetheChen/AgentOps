# 排版与动画设计原则

Hyperframes HTML 编排的质量指南。涵盖设计系统、反AI味视觉、动画决策、排版规范。

---

## 这是视频，不是PPT

正在做的是**视频网页**——观众感觉是在看视频，不是在看翻页幻灯。

判断每一步做对没有，标准非常朴素：

- **不像PPT** —— 页面中不得包含页眉页脚，突出主视觉元素
- **看起来舒服** —— 配色、字体、节奏都让人放松，不得出现大量纯文字、小号字体
- **有视觉冲击** —— 画面在演事情，不只是文字堆砌，不得一次性全部罗列，关键元素随进度逐步推进

---

## 设计系统先行（写代码前必须声明）

**写第一行代码前**，先明确以下 6 个维度：

```markdown
设计决策：
- 配色方案：[主色] / [次要色] / [中性色] / [强调色]
  （优先从 Step 3 生成的图片中提取主色调，保证音画色调一致）
- 字型等级：hero ≥ 80px / 标题 ≥ 48px / 正文 ≥ 24px / 角标 ~14px
- 留白系统：舞台四边 ≥ 80px 安全区
- 圆角策略：0px（尖锐/报纸感）/ 4px（精炼）/ 16px（柔和）/ 32px（keynote感）
- 阴影层级：无（扁平）/ offset实色（bauhaus）/ 纸浮（柔和）
- 动效风格：{电影感慢速 / 弹簧活力 / 利落线性 / 安静淡入}
```

### CSS Token 规范

所有颜色和字体家族**必须用 CSS 变量**，禁止硬编码 hex/rgb/字体名：

```css
:root {
  /* 颜色 */
  --bg-primary: #0a0a0a;
  --bg-card: rgba(255,255,255,0.05);
  --text-primary: #f0f0f0;
  --text-mute: #888;
  --text-faint: rgba(255,255,255,0.3);
  --accent: #ff6b35;
  --accent-glow: rgba(255,107,53,0.3);
  --rule: rgba(255,255,255,0.1);
  
  /* 字体 */
  --font-display: 'Playfair Display', serif;   /* hero 大字 */
  --font-body: 'Inter', sans-serif;             /* 正文 */
  --font-mono: 'JetBrains Mono', monospace;     /* 数据/代码 */
  
  /* 舞台 */
  --stage-pad: 80px;
  --card-radius: 4px;
}
```

---

## 反AI味视觉指纹（全部禁止）

这些是AI生成网页的共有"指纹"，**全部不要**：

| 禁止 | 原因 | 正确替代 |
|------|------|---------|
| **紫粉/蓝紫对角渐变背景** | AI训练数据收敛的"科技感"公式，任何品牌穿上都一样 | 从场景图片提取主色调，或使用深灰/暖墨等中性色 |
| **圆角卡片+彩色左边框** | Material/Tailwind时代残留，现在已是视觉噪音 | 用分割线(--rule)、光影层次、留白区分 |
| **渐变按钮+大圆角药丸** | 万能模板，和"有设计感"正好相反 | 文字CTA、下划线hover、极简轮廓按钮 |
| **emoji当图标** | "没图标就贴emoji"的AI习惯 | placeholder占位卡 `[icon]`：方框+标签 |
| **假数据/假logo/假"X万用户"** | 损害可信度，观众会注意到数字对不上 | 承认缺、标记 `[真实数据待填]` |
| **CSS手绘假插画/假产品** | AI画的SVG人脸总是比例失调，感觉廉价 | 用Step 3生成的MMX图片，或诚实的placeholder |
| **全场一种入场动画** | AI味最稳定的指纹（全场fade/blur） | 每步动作不同，优先内容驱动动画 |
| **每步都挂持续微动** | ken burns/光晕呼吸/持续闪烁=机器节奏 | 动作演完就停，静止也是一种设计语言 |
| **Inter/Roboto/Arial作展示字体** | 太常见，读作"demo页"而非"设计过的产品" | 选择有性格的字体（但不超过2个字体家族） |
| **右下角mono角标/序号** | 又一个AI共性，每个视频都挂角标 | 仅在有信息价值时用（如来源标注），否则去掉 |

---

## Placeholder 哲学

**缺图标/图片/数据时，placeholder 比 fake 专业一百倍**：

- 缺图标 → 方框+标签（`[icon]`、`▢`）
- 缺头像 → 首字母圆圈+填色
- 缺图片 → placeholder卡片+比例信息（`16:9 image`）
- 缺数据 → 主动问用户，不编
- 缺logo → **停、问用户**，不用"品牌名+彩色方块"代替

> Placeholder 传达"真东西来了就能上"。Fake 传达"我在糊弄"。

---

## 动画原则

### 内容驱动动画决策（核心方法论）

**优先级**：

1. **先找内容内在动作** —— 看这一步口播在说什么，内容本身有没有"动"的理由：
   - 数字在讲增长 → 数字递增动画
   - 排名在讲变化 → 排名交换/位移
   - 流程在讲步骤 → 节点依次点亮/连线自绘
   - 对比在讲差异 → 一刀切开/聚光灯扫过/形状变形
   - 数据在讲分布 → 横条生长/饼图展开/热力图浮现
   - 概念在讲聚合 → 粒子聚拢成形
   - 代码/命令 → 模拟终端逐行输出
   - AI对话 → 模拟对话窗口逐条弹出

2. **找不到内容动作，才用入场动画兜底**：
   - fade（淡入）
   - blur clear（模糊→清晰）
   - slide（滑入）
   - scale（缩放进入）

3. **禁止全场景一种动画**。如果 3 步都用 fade = 回去重新设计。

4. **限制持续微动**。ken burns（缓慢缩放）、光晕呼吸、持续闪烁 = AI味。能停就停。

### 逐步揭示铁律

> 口播在说"第一是X、第二是Y、第三是Z"这种清单/列表时，**严禁**一个 step 把 X/Y/Z 全部 stagger 上来。

- 一项 = 一个 step
- X 只在它自己的 step 里独自亮起
- 讲到 Y 时，X 灰化保留作上下文 + Y 亮起
- 讲到 Z 时，X/Y 都灰化 + Z 亮起

**判断标准**：讲者会一个一个念出来吗？会 → 必须逐个揭示。

### 动画节奏参考

| 内容类型 | 参考动画时长 | 缓动 |
|---------|------------|------|
| 数字递增 | 0.6-1.2s | `cubic-bezier(0.22,0.61,0.36,1)` 弹入 |
| hero文字入场 | 0.5-0.8s | `cubic-bezier(0.33,1,0.68,1)` 缓出 |
| 横条生长 | 0.4-1.0s | `cubic-bezier(0.65,0,0.35,1)` 先快后慢 |
| 排名位移 | 0.3-0.6s | `cubic-bezier(0.34,1.56,0.64,1)` 弹性 |
| 节点点亮 | 0.2-0.4s/节点 | `ease-out` |
| 卡片入场 | 0.3-0.5s | `cubic-bezier(0,0,0.58,1)` |

### 代码红线

- **不用 `setTimeout`/`setInterval` 驱动动画** —— 用 CSS keyframes 或 `animation-delay` 串行
- 可交互元素加 `data-no-advance`，避免点击时误推进 step
- 全场景用 CSS 变量提取设计 token，禁硬编码颜色/字体

---

## 必须用 CSS / SVG / Canvas / JS 大胆绘制视觉演示

> **这是底线。** 每一场景都至少要有 1-2 处"动起来的图/演示元素"。
> **整场景只有纯文字 = 验收不过 = 回去重做。**

视频感最强的来源 —— 用户**看见**了被讲解的东西在屏幕上演给他看：

| 技术 | 适用场景 |
|------|---------|
| **CSS @keyframes** | 80%的动画需求：入场/数字变换/横条/卡片/过渡 |
| **SVG** | 连线自绘/形状变形/流程点亮/图标动画 |
| **Canvas** | 粒子效果/噪声背景/数据可视化/自定义图表 |
| **JS 驱动** | 终端模拟/对话窗口/排名交换/复杂数据动画 |

**组合发挥都行 —— 但每场景必须用。不允许整场景纯文字。**

---

## 排版规范

### 字号层级

| 元素 | 最小字号 | 建议 |
|------|---------|------|
| hero数字 | 100px | 120-200px，趁大趁粗 |
| hero标题 | 80px | 80-120px，一行撑满 |
| 副标题 | 36px | 补充hero信息 |
| 正文/列表 | 24px | ≥24px，远观可读 |
| 数据卡片值 | 64px | 厚重数字 |
| 数据卡片标签 | 22px | 配数据值 |
| 信息池角标 | 14px | mono字体，低调不抢戏 |

### 留白规范

- 舞台四边 ≥ 80px 安全区（1920×1080）
- 文字区块之间留白 ≥ 字号的 0.5 倍
- hero 区域独占视口中心 40-60%
- 宁可留白多不可信息塞满

### 内容密度

每个 step 屏幕上只挂这个节拍**最值得放大的 1-3 个东西**：
- 一个 hero 标语
- 一个数字
- 一组对比
- 必要的视觉演示

不要试图把原文每个字都搬上去。那是论文阅读，不是视频。

---

## 双源原则：画面信息 > 口播信息

> 如果画面等于把口播打字打了一遍——那就是 PPT，不是视频。
>
> 画面必须回去翻 search-results.md/storyboard 信息池，挂上口播没念的细节：

- 右下角 mono 来源角标（"来源：GitHub Trending 2026W21"）
- 副标小字补充数据（口播说"涨了很多"，画面写"+3280★，周环比 +47%"）
- pull-quote 引用原文（口播说"社区反响强烈"，画面挂真实评论截图）
- 对比维度细化（口播说"比第二名强"，画面展示4维度对比表）

---

## 完工自检（写完整段HTML后强制执行）

- [ ] 每场景 ≥ 1-2 处 CSS/SVG/Canvas/JS 视觉演示 —— 没有 = 回去补
- [ ] 不同 step 的主导动作不一样 —— 全场景一种动画 = 回去重做
- [ ] 字号大（hero ≥ 80px, body ≥ 24px）、留白舒服、配色舒服
- [ ] 清单/列表逐个揭示，**1 项 = 1 step**
- [ ] 画面信息比口播稿多（回了 search-results.md 抽细节挂上来）
- [ ] 没有紫粉渐变/圆角彩色边框/emoji当图标/假数据/假logo
- [ ] 缺的素材用 placeholder，不是 fake
- [ ] 颜色和字体家族全部走 CSS 变量（无硬编码 hex/字体名）
- [ ] 禁止小号字体（<20px）、大量纯文字（出现后必须回去改）
- [ ] 禁止任何形式的页眉页脚，仅展示关键内容
- [ ] hyperframes API 铁律全部就位（见下方 API 规范章节）
- [ ] 交付时**主动告诉用户**："本视频还缺这些素材"

---
## hyperframes API 规范（必读，不遵守渲染必挂）

> **这是硬性 API 契约，不是设计建议。缺一项渲染直接报错。**
> 
> 好消息：render 工具会自动补一些（root 属性、timeline 骨架、track 去重），
> 但你写得越合规，渲染越稳定。

### 1. HTML 结构最低要求

```html
<!doctype html>
<html>
<head><meta charset="utf-8"><style>/* CSS */</style></head>
<body>
<div
  data-composition-id="my-video"
  data-width="1920"
  data-height="1080"
  data-start="0"
>
  <!-- 所有 clip 元素放这里 -->
  <img class="clip" data-start="0" data-duration="5" data-track-index="0" src="media/images/scene-01.png">
  <div class="clip" data-start="0" data-duration="5" data-track-index="1">...</div>
  <audio data-start="0" data-duration="5" data-track-index="10" src="media/audio/scene-01.mp3"></audio>
</div>
</body>
</html>
```

**铁律**：
- `data-composition-id`、`data-width`、`data-height`、`data-start="0"` 必须在根 div 上
- 所有可见元素必须有 `class="clip"` + `data-start` + `data-duration` + **互不冲突的 `data-track-index`**
- 音频必须用 `<audio>` 标签（不能用 `<video>` 带音轨），src 路径用 `media/audio/scene-NN.mp3`
- 图片 src 用 `media/images/scene-NN.png`
- 不同 track 的 clip 可同时播放；同 track 的 clip 时间不能重叠

### 2. track 分配策略（避免 lint 报 `overlapping_clips_same_track`）

```
track 0: 场景背景图片
track 1: 渐变遮罩
track 2: 文字/卡片内容
track 3-9: 其他装饰元素
track 10-11: 音频（每个场景各分配一个）
```

**场景 2 的元素必须用不同的 track 索引**（不能用 track 0/1/2，因为场景 1 已经用了）。

### 3. 音频处理

- 旁白 mp3 用 `<audio>` 元素，src 指向 `media/audio/scene-NN.mp3`
- 实测时长写 `data-duration`（从 durations.json 取，**禁止手填**）
- 如果没有背景音乐，不要写空的 `<audio>` 元素

### 4. GSAP / 动画

- GSAP CDN：`<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.7/dist/gsap.min.js"></script>`
- 所有 timeline 必须 `{ paused: true }`，注册到 `window.__timelines["<composition-id>"]`
- render 工具会自动注入骨架；如果你自己写了 GSAP 动画，确保 `window.__timelines` 注册了
- 禁止异步构建 timeline（`setTimeout`/`async/await`/Promise 内）
- 禁止 `repeat: -1`

### 5. 视频/音频分离

如果用了 `<video>`，必须 `muted playsinline` + 单独 `<audio>`：

```html
<video class="clip" data-start="0" data-duration="10" data-track-index="0"
       src="video.mp4" muted playsinline></video>
<audio data-start="0" data-duration="10" data-track-index="10" src="video.mp4"></audio>
```

### 6. 禁止事项

- 禁止 root div 套 `<template>` 标签（那是子组件的写法，standalone 不需要）
- 禁止 `<br>` 断行（用 `max-width` 自然换行）
- 禁止 `gsap.set()` 给后面的 clip 元素（它们在该时间点还没创建）
- 禁止动画出画后再接转场（转场就是出口动画）