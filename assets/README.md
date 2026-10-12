# Porter 宣发视频

`porter-promo.mp4` — Porter 产品发布宣传片（51 秒，1280×720，H.264）。

## 内容结构

1. **Hook**（0–5s）：「换了 5 个 AI 编程工具，记忆每次都从零开始？」
2. **Porter 是什么**（5–13s）：你 → 27 个 Harnesses → MCP → Porter 记忆库，一图讲清架构
3. **实测**（13–33s）：真实 `scripts/demo_e2e.py` 输出——跨 harness 记忆召回、待确认队列、撤销即时生效
4. **面板展示**（33–57s）：真实 UI 截图慢速平移——待确认队列、记忆库、自动同步、记忆星图
5. **收尾**（57s–）：「Porter · 数据只属于你」+ GitHub 地址

## 设计语言

深暖炭底 + 暖金 `#D4A24E` 强调色 + 中文衬线标题，中英双语。无配音（默认静音观看），无 AI 生成人物/语音，所有界面均为真实 Porter 截图。

## 文件

- `porter-promo.mp4` — 成片（faststart，可直接网页播放）
- `poster.png` — 封面帧
- `render_promo.py` — 生成脚本（PIL + ffmpeg，可复现）
