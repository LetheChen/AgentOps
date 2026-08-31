---
name: subagent-dispatch
description: 派发子任务标准闭环——trigger_workflow + collect_child_result + present_content + finalize
domain: _shared
depends_on: [dag-ops]
---

# 派发子任务标准闭环

> 本 skill 教你（Manager Agent）如何正确派发子 agent 处理用户请求，确保子任务结果不被闷在内存里。

---

## 一、标准闭环（必须遵守）

当决定派发子 agent（如 content_curator / smart_query / log_analyst 等）处理用户请求时，**必须**按以下闭环收尾：

1. 调 `trigger_workflow(run_mode="conversational"|"task", agent_id="xxx", initial_message="...")` 拿到返回的 `run_id`

2. **立即**调 `collect_child_result({run_id: "<刚才的 run_id>"})` 阻塞等待子 run 到达终态（completed/failed/cancelled/dormant；超时 600 秒自动返回当前状态）

3. 拿到 `messages` + `summary` + `final_outputs` 后，**整合告诉用户**

4. 需要可视化（表格/指标/进度）时用 `present_content` 推整合结果，纯文本结论直接对话回复；只有「必须等用户补充信息才能继续」的场景才用 HIL 表单

---

## 二、反例与正例

### 反例（绝对禁止）

```
trigger_workflow → 对话消息 "任务已启动 · run_id=xxx" → finalize
```

→ 子 agent 处理完的结果被闷在它自己的内存 / messages 表里，用户什么都看不到

### 正例（标准闭环）

```
trigger_workflow → collect_child_result（拿到结果）→ present_content 或对话回复（整合）→ finalize
```

→ 子 agent 的最终交付会被合并进 manager 的回复，用户能看到完整结果

---

## 三、例外

用户明确说"先放着后面再看"时省略 `collect_child_result`，但**必须**告知"已派发，结束后会自动推送"。
