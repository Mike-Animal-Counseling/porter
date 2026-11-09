[English](ui-redesign-spec.md) | **中文**

# Porter 面板 UI 重设计规范

**状态：** 调研完成，可进入实现
**范围：** 仅重设计属主面板的视觉（`porter/panel.py` + 新增 `porter/static/`）。除非另有说明，不改动路由/逻辑。保持服务端渲染 HTML + HTMX——不上 SPA 框架。
**目标质感：**"2026 年 AI 基础设施产品"（Linear / Vercel / Supabase / Langfuse 的调性），而不是 "2015 年的 Bootstrap"。

研究过的参考产品：Linear（深色优先、LCH 色彩、倒 L 型 chrome、密集列表）、Vercel（零饱和度、Geist、状态圆点内联）、Railway（分层深色表面、带状态圆点的服务卡片）、Supabase（扁平深色、暖灰、单一薄荷绿强调色、ID 用等宽字体、32px 密集表格行、发丝分隔线）、Langfuse/LangSmith（可观测性 IA：侧边导航、主从式 trace、密集 observation 表格、全局时间范围筛选）、shadcn（CSS 变量式 token 主题：background/foreground/card/primary/muted/border/radius 成对）、HTMX+服务端渲染仪表盘模式（HTML 片段走线传输，无客户端状态仓库）。

---

## 1. 设计语言

### 1.1 色彩系统 —— 深色优先、单一强调色

深色是默认（Linear/Supabase 共识：基础设施工具深色优先；我们的用户整天泡在深色编辑器里）。浅色模式通过 token 切换支持，开关放在 header。

**主强调色：靛蓝紫 `#6E6BF2`**（Linear 系 `#5E6AD2`，针对深色背景略提亮）。节制使用：主按钮、激活的导航指示、焦点环、关键链接。只用一个强调色——状态色是语义色，绝不装饰。

**状态色**（永远圆点 + 文字，绝不只用颜色）：
- 成功/健康/活跃：`#34D399`（绿）
- 待定/注意：`#FBBF24`（琥珀）
- 危险/已撤销/错误：`#F87171`（红）
- 信息/同步中转：`#60A5FA`（蓝）

### 1.2 深色 token（`:root`）

```css
--bg:            #0B0C0E;  /* 应用画布 —— 近黑，不是纯 #000 */
--bg-subtle:     #101214;  /* 侧边栏 */
--surface:       #14171B;  /* 卡片 */
--surface-raised:#1B1F24;  /* 弹窗、对话框、悬停态 */
--border:        #232A31;  /* 发丝分隔线 */
--border-strong: #313A43;  /* 输入框边框、卡片悬停描边 */
--text:          #EDEFF2;  /* 主文本 */
--text-secondary:#9BA3AD;  /* 次文本 */
--text-tertiary: #646D78;  /* 占位符、禁用态 */
--accent:        #6E6BF2;
--accent-hover:  #8280F5;
--accent-muted:  rgba(110,107,242,.14);  /* 染色背景，尽量用 color-mix */
--accent-text:   #C7C5FF;  /* 深色上的强调色文字 */
--success: #34D399; --warning: #FBBF24; --danger: #F87171; --info: #60A5FA;
--shadow: 0 1px 2px rgba(0,0,0,.35);
```

### 1.3 浅色 token（`[data-theme="light"]`）

```css
--bg: #FAFAF9; --bg-subtle: #F4F4F2; --surface: #FFFFFF; --surface-raised: #FFFFFF;
--border: #E8E6E3; --border-strong: #D6D3CF;
--text: #1B1D21; --text-secondary: #5B616B; --text-tertiary: #9AA0A8;
--accent: #5A58E8;  /* 浅色上加深以保证对比度 */
--accent-hover: #4A48D8;
--accent-muted: rgba(90,88,232,.10);
--accent-text: #4A48D8;
--shadow: 0 1px 2px rgba(20,20,30,.06);
```

### 1.4 字体

- **UI 字体：** 系统字体栈（离线、中文渲染正确——关键，因为中文是默认语言）：
  `-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", Inter, "Segoe UI", sans-serif`
- **等宽**（ID、哈希、cursor、时间戳、token、代码）：`ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Consolas, monospace`
- **字号阶梯：** 12（元信息/标签）/ 13（表格正文、次要）/ 14（正文基准）/ 16（卡片标题）/ 20（页面标题）/ 24（统计数字）/ 32（仅 hero 数字）
- 页面标题：20px，字重 600，letter-spacing -0.01em。UI chrome 里不用粗黑（上限 600）。
- 标签/眉题：12px，大写，letter-spacing .06em，`--text-tertiary`（如 "SYNC STATUS"、"DANGER ZONE"）。

### 1.5 间距与圆角（8px 基准）

- 页面内边距：24px（≥1400px 时 32px）。区块间距：24px。卡片内边距：20px。密集控件内边距：8–12px。
- 圆角：`--radius-sm: 6px`（输入框、小按钮）、`--radius-md: 8px`（按钮、表格容器）、`--radius-lg: 12px`（卡片、对话框）、`--radius-full: 999px`（胶囊/徽章）。
- 边框：只用 1px 发丝线；**卡片不用重投影**——用背景色阶区分表面（`bg` → `surface` → `surface-raised`），学 Supabase。

### 1.6 侧边栏 vs 顶栏 —— 决策：侧边栏

**理由：** 7 个页面是侧边栏的体量（Vercel 在 2026 年*改回了*侧边栏；Supabase、Langfuse、Linear 都是左导航）。纯顶栏撑不起 ~5 个以上的条目，还浪费纵向空间。侧边栏还能给收件箱计数徽章（最高频信号）一个永久位置，以及专注模式的折叠入口。

---

## 2. 布局外壳

```
+--------+------------------------------------------------------+
|        |  header: 面包屑 · 搜索 ⌘K · 主题 · 语言 · 版本        |
| side   +------------------------------------------------------+
| bar    |                                                      |
| 248px  |  页面内容（max-width 1200px，24px 内边距）             |
|        |                                                      |
+--------+------------------------------------------------------+
```

**侧边栏**（`<aside>`，`--bg-subtle`，无右边框——用色调而非线条分隔）：
- 顶部：Porter wordmark（文字 "Porter" 15px/600 + 小字 "MEMORY LAYER" 眉题，或一个极简 SVG 标志）。不需要 logo 图片。
- 导航项（图标 + 标签，14px，8px 圆角行，整行可点击）：
  1. Memories（记忆）
  2. Inbox（待确认队列）——带琥珀色计数徽章（pending N）
  3. Sync（同步）——带状态圆点（绿=全部在线 / 琥珀=延迟）
  4. Clients（授权）
  5. Audit（审计）
  6. Snapshots（快照）
  7. Settings（设置，底部独立分组，或 "SYSTEM" 分区标签）
- 激活项：`--accent-muted` 背景 + `--accent-text` 标签 + 左缘 2px 强调条（或微妙的左指示器）。未激活：`--text-secondary`。
- 底部：版本胶囊（`v0.1.0`，等宽 11px，弱化）+ 主题开关在 header，不在侧边栏。
- <1100px 时可折叠为 64px 图标栏（localStorage 持久化）；<720px 经由 header 的汉堡菜单变为覆盖式抽屉。

**Header**（sticky，`--bg` 带模糊，56px）：
- 左：汉堡菜单（仅移动端）+ 面包屑（`Porter / Memories`）或页面标题。
- 中/右：搜索框（⌘K 风格，200px，经 HTMX 过滤当前页；v1 做完整命令面板是过度设计）、主题开关（太阳/月亮 SVG）、语言切换（紧凑的 `中文/EN` 分段控件——取代仅设置页下拉框，成为主控件；设置页保留完整卡片）、版本胶囊。
- 没有用户头像/菜单——单属主产品，无登录。不要发明账号 chrome。

**响应式：** ≥1100px 完整侧边栏；720–1100px 图标栏；<720px 抽屉 + 表格横向滚动（绝不重排表格列——Supabase 规则）。

**i18n 备注：** 侧边栏标签和所有 chrome 都走现有 `t()`；中文字体栈已覆盖渲染。`<html>` 上已设置 `lang` 属性。

---

## 3. 各页面线框图

### 3.1 Memories（记忆）—— 资料库

密集、可搜索的表格。Supabase 表格编辑器的调性。

```
+---------------------------------------------------------------+
| Memories                          128 memories   [+ New]      |
| [search memories...        ] [Type ▾] [Tag ▾] [Status ▾]      |
+---------------------------------------------------------------+
| CONTENT              | TYPE   | TAGS        | SOURCE | UPDATED |
| user avoids cilantro…| pref   | ● dietary   | codex  | 2h ago  |
| API returns snake_…  | pref   | ● code_style| cursor | 1d ago  |
+---------------------------------------------------------------+
| Semantic search only returns ACTIVE memories… (footnote)       |
+---------------------------------------------------------------+
```

- 筛选条：header 下 sticky；搜索走 HTMX（`hx-get=/panel/memories/rows`，`hx-trigger="input changed delay:300ms"`，`hx-target="#mem-table-body"`，`hx-indicator` 表格骨架闪）。
- 行：48px，13px，发丝分隔线，悬停 `--surface-raised`。内容列：2 行截断，链接样式 → 打开**右侧滑出详情面板**（480px 抽屉）：完整内容、元数据网格（id 等宽 + 复制按钮、类型、标签、置信度条、来源路径、创建/更新时间）、**历史链**（superseded_by 时间线——"edited from v3 → v4"）、操作（Edit / Archive / Delete）。
- 状态胶囊：active=绿，pending=琥珀，archived=灰。
- 标签：小染色 chip（中性单色调，不用彩虹——靠文字区分）。
- 空状态：居中图标 + "No memories yet" + "Import from a harness or add your first memory" + [Import] 按钮。

### 3.2 Inbox（待确认队列）—— 最高频页面，triage 优先

这是属主访问最多的页面。按邮件 triage / Linear inbox 设计：**一张卡片一个决策，最少 chrome，最快批准**。

```
+---------------------------------------------------------------+
| Inbox (3)                      [Select all] [Approve all (3)] |
| ┌ Card ──────────────────────────────────────────────────┐   |
| │ ● code_style · from claude-code · conf 0.70 · 10:42    │   |
| │                                                        │   |
| │   User prefers Tailwind for frontend styling           │   |
| │                                                        │   |
| │                    [Approve] [Edit & approve] [Reject]  │   |
| └────────────────────────────────────────────────────────┘   |
| ┌ Card ──────────────────────────────────────────────────┐   |
| │ ...                                                    │   |
+---------------------------------------------------------------+
```

- 卡片（不是表格行）：内容是主角（15px），元信息是安静的 12px 眉题行（标签 chip · 来源客户端 · 置信度 · 时间）。
- 操作右对齐：**Approve**（主强调色按钮）、**Edit & approve**（次按钮）、**Reject**（ghost，悬停变红）。全部 HTMX POST → 卡片带短暂成功闪光换出；剩余计数更新；清空后 → 庆祝式空状态（"Inbox zero — all caught up"）。
- 置信度显示为元信息下的细进度条（不只是一个数字）。
- "Approve all" 带 `hx-confirm`（"Approve all 3 pending memories?"）。
- 键盘：`a`/`e`/`r` 快捷键是 nice-to-have；v1 跳过，把按钮做大做快。

### 3.3 Clients（授权）

```
+---------------------------------------------------------------+
| Clients                                   [+ Register client] |
+---------------------------------------------------------------+
| ┌ codex ───────────────● active──────────────[Revoke] ┐        |
| │ porter_8f3k… (mono, copy btn)                       │        |
| │ scopes: [memory.read] [memory.write]  last used 2h  │        |
| └────────────────────────────────────────────────────┘        |
| ┌ cursor ──────────────● revoked──────────[deleted]   ┐        |
| │ ...                                                 │        |
+---------------------------------------------------------------+
```

- 每个客户端一张卡片：名称（16px/600）+ 状态圆点/胶囊，client_id 等宽 + 复制按钮，scope 徽章（染色 chip），"last used" 相对时间。
- Revoke：破坏性描边按钮 → `hx-confirm` 对话框（"Revoke all tokens for 'codex'? Active sessions will be terminated immediately."）。
- Register → 弹窗（`<dialog>`）：表单（名称、scope 复选框）→ 响应中**只显示一次**密钥，等宽块 + 复制按钮 + "I saved it" 确认。这是唯一适合用弹窗的地方（密钥仪式）。

### 3.4 Audit（审计）

Langfuse Observations 调性：密集、可筛选的事件流。

```
+---------------------------------------------------------------+
| Audit                                    [1h|24h|7d|30d] [CSV] |
| [actor ▾] [action ▾] [search...                      ]        |
+---------------------------------------------------------------+
| TIME     | ACTOR  | ACTION          | DETAIL                    |
| 10:42:11 | owner  | memory.approve  | 13236885… → active        |
| 10:41:03 | codex  | memory.add      | pending_review (2 tags)     |
+---------------------------------------------------------------+
```

- 40px 行，等宽时间戳，action 按类别染色 chip（写入=蓝，批准=绿，撤销/删除=红，认证=灰）。
- 点击行 → 行内展开 JSON 详情（HTMX 换出，不跳转）。
- 时间范围分段控件联动所有查询（Langfuse 模式）。导出 CSV 按钮。
- "不可变"提示：小锁图标 + "Append-only — entries cannot be edited or deleted" 说明。信任信号。

### 3.5 Snapshots（快照）

```
+---------------------------------------------------------------+
| Snapshots                              [+ Create snapshot]    |
+---------------------------------------------------------------+
| ┌ porter-2026-10-11.porter ──────────── ✓ signed ┐             |
| │ 128 memories · 2.1 MB · AES-256-GCM · 10:42   │             |
| │ [Download] [Inspect] [Apply]                  │             |
| └───────────────────────────────────────────────┘             |
+---------------------------------------------------------------+
```

- 卡片：文件名（等宽）、签名徽章（绿 ✓ signed / 琥珀 "legacy unsigned"）、元信息行、操作。
- Create → 弹窗：名称、加密开关（默认开）、Ed25519 签名说明。
- Apply → 确认对话框，**先做 dry-run 预览**（计数："will add 12, update 3, skip 113"）——破坏性操作，双重确认。
- Inspect → 抽屉展示内容 JSON 树。

### 3.6 Sync（同步）—— 差异化页面

这个页面是卖点（"自动跟随"）。Railway 服务卡片调性 + 状态仪表盘。

```
+---------------------------------------------------------------+
| Sync                                        head seq #4821    |
| [stat: 3 online] [stat: 0 lagging] [stat: median lag 4s]      |
+---------------------------------------------------------------+
| ┌ ● codex ───────── online ───────────── cursor #4819 ┐       |
| │ lag: 2 changes · ▂▅▂▄▆ (sparkline) · last seen 12s │       |
| └────────────────────────────────────────────────────┘       |
| ┌ ● cursor ───────── online ───────────── cursor #4821 ┐      |
| │ lag: 0 · up to date · last seen 5s                  │      |
| └────────────────────────────────────────────────────┘       |
| ┌ ○ windsurf ─────── offline ────────────────────────┐       |
| │ last seen 2h ago · cursor #4790 (31 behind)         │       |
| └────────────────────────────────────────────────────┘       |
| How it works: adapters poll GET /sync/delta?since=<cursor>…   |
+---------------------------------------------------------------+
```

- 头部统计：adapter 在线数、延迟数、中位延迟——24px 数字，安静的标签。
- Adapter 卡片：状态圆点（绿 <5min，琥珀 5–60min，灰 >60min/离线）、名称、cursor 等宽、延迟行（"2 changes behind" 或 "up to date"）、last-seen 相对时间。
- **延迟 sparkline：** 纯 CSS 条形 sparkline（最近 20 个延迟样本画成 inline-block div，3px 宽，高度缩放）——不用图表库。服务端根据近期样本渲染。
- 底部说明卡片："How sync works"——3 步（adapter 写入 → hub 分配序号 → 其他方轮询 `/sync/delta`），等宽 endpoint，链接到 DEVELOPER.md。这是在教心智模型。
- 自动刷新：统计/卡片容器 `hx-get` 每 15s 轮询（`hx-trigger="every 15s"`），静默换出。

### 3.7 Settings（设置）

分组卡片，max-width 720px（表单窄一点更好读）：

1. **Language** 卡片 —— 中文/EN 分段控件（header 里也有；这里是完整设置）。
2. **Scope tags** —— 预设 + 自定义标签表格（现有），内联新增表单。
3. **Backup** —— 导出按钮（porter-export/v1），快照快捷链接。
4. **Danger zone** —— 红色调卡片（`rgba(248,113,113,.06)` 背景，红边框）："Delete all memories"、"Reset instance"——每个都 `hx-confirm`，reset 还要打字确认。

---
## 4. 组件模式（做一次，到处复用）

| 模式 | 规范 |
|---|---|
| **Buttons** | 4 种变体：`primary`（强调色底，白字，悬停加深）、`secondary`（surface-raised 底，边框）、`ghost`（透明，悬停 surface）、`danger`（透明，红字；悬停红染）。高 36px，内边距 0 16px，圆角 8px，14px/500。焦点：2px 强调色环，偏移 2px。|
| **Inputs** | 36px，`--surface-raised` 底，1px `--border-strong`，圆角 8px，14px。焦点：强调色边框 + 光环。占位符 `--text-tertiary`。|
| **Tables** | 13px 正文，12px 大写表头标签（`--text-tertiary`），48px 行（audit 40px），发丝行分隔线，滚动时表头 sticky，行悬停 `--surface-raised`。ID/哈希/时间戳用等宽。|
| **Badges/pills** | 12px/500，`radius-full`，内边距 2px 10px，经 `color-mix(in srgb, <color> 14%, transparent)` 染色底 + 彩色文字 + 6px 圆点表示状态。绝不只用颜色。|
| **Cards** | `--surface` 底，1px `--border`，圆角 12px，20px 内边距。不用阴影（用色调分隔）。悬停：可点击卡片用 `--border-strong`。|
| **Empty states** | 居中：40px 弱化 SVG 图标，15px/600 标题，13px 次要描述，主 CTA 按钮。纵向留白慷慨（64px）。|
| **Toasts** | 右下角堆叠，`--surface-raised` 底，`--border-strong`，圆角 12px，4s 自动消失，类型：success（绿条）/error（红条）/info。触发：服务端发 `HX-Trigger: {"toast": {"type":"success","msg":"..."}}` → 10 行 JS 监听器。|
| **Confirm dialogs** | 简单场景用 HTMX `hx-confirm`（revoke、approve-all）。仪式性场景用原生 `<dialog>` 元素（带样式）（客户端密钥展示、带 dry-run 预览的快照应用）。不用 JS 弹窗库。|
| **Filter bars** | Sticky（`position: sticky; top: 56px`），`--bg` 模糊底，纵向 12px 内边距，统一结构：搜索框 + 下拉 + 右对齐的结果计数。|
| **Skeletons** | HTMX 换出时用：`.htmx-indicator` 闪光块（脉动的 `--surface-raised` 条，贴合行形状）。不要只有 spinner 还导致布局跳动。|
| **Copy buttons** | 任何等宽 ID/密钥旁的小 ghost 图标按钮；点击 → 剪贴板 + "Copied" toast。总共约 15 行 JS。|
| **Drawers** | 右侧滑出 480px，用于详情视图（记忆详情、快照 inspect）。`position: fixed`，`--surface` 底，`--border-strong` 左边框，遮罩 `rgba(0,0,0,.5)`。Esc / 点遮罩关闭。可以是 HTMX 换出的内容。|

**图标：** 单一集合，内联 SVG，默认 16px，`stroke="currentColor"`，线宽 2，
Lucide/Feather 路径词汇（search、plus、check、x、clock、shield、key、database、refresh-cw、
download、upload、settings、globe、moon、sun、copy、chevron-down、trash、edit、zap、activity）。
实现为 Python `icon(name, size=16)` helper 返回 SVG 字符串——任何地方都不用 emoji 图标。

---

## 5. CSS 方案 —— 推荐

**推荐：手写 CSS + shadcn 风格 CSS 变量。不用 Tailwind，不用 CDN。**

这个代码库的理由：
1. **离线优先是产品价值**（"数据默认不出本地"）。Tailwind Play CDN 每次页面加载都要联网——理念上就不对，还破坏了 air-gapped 自托管。带构建步骤的 Tailwind 则给一个 Python 项目的发布流程加了 Node。
2. **单一事实源头：** 一个 `porter/static/css/app.css`（约 600–900 行）+ `tokens.css`。
   每个模板引用语义类（`.btn-primary`、`.card`、`.badge-success`）——Python 保持可读，
   中英 i18n 不受影响（CSS 与语言无关）。
3. **深浅色主题**从变量系统自然长出来：`:root` = 深色，`[data-theme="light"]` 覆盖。不用写两遍类。
4. **HTMX 已 vendor**——保持"零网络依赖"属性：经 FastAPI `StaticFiles` 提供 `/static`，带 immutable 缓存头。

**文件布局：**
```
porter/static/
  css/tokens.css      # :root + [data-theme=light] 变量、字体、基础 reset
  css/app.css         # 布局外壳、组件、各页面样式
  js/htmx.min.js      # vendored（已在用——挪到本地，去掉 CDN）
  js/panel.js         # 约 60 行：主题开关、语言分段控件、
                      #   toast 监听器（HX-Trigger）、复制按钮、
                      #   dialog helper、侧边栏折叠
porter/panel.py       # 新增：layout() 外壳函数、icon() helper，各页面函数用 class
```

**给实现 agent 的规则：**
- 模板里零 `style=""` 属性；`tokens.css` 之外零硬编码 hex。
- 类名：BEM-lite（`.mem-table`、`.mem-table__row`、`.btn`、`.btn--primary`、`.badge`、`.badge--success`）。
- 主题默认：深色；开关写 `localStorage("porter-theme")` + 在 `<html>` 上设 `data-theme`；只在首次访问时尊重 `prefers-color-scheme`。
- 所有可交互元素键盘可达；处处 `:focus-visible` 强调色环。
- 对比度：双主题下正文与表面对比度 ≥ 7:1（实现时核对 token 对）。

**不要做的事：** 不要加 React/Vue/Svelte；不要加 CSS 构建步骤；不要拉 icon 字体（只用内联 SVG）；不要用 `<table>` 做卡片布局，也不要用 div 做数据表格（语义对 a11y 重要）。

---

## 6. Don'ts（让基础设施仪表盘显旧的做法）

1. **纯 `#000` 背景**——用带色调的近黑（`#0B0C0E`）；纯黑显廉价，白字还会"震动"。
2. **超过一个强调色**——选定 indigo，删掉其余。状态色是语义色，不是装饰。
3. **Bootstrap 味：** 默认蓝 `#0d6efd`、到处 4px 圆角、灰 `#6c757d` 次文本、通用卡片阴影。一秒回到 2015。
4. **无目的的渐变**——尤其是 chrome 上的紫蓝 "AI 渐变"。扁平染色表面更高级。
5. **卡片套卡片套卡片**——最多一层嵌套；用间距分组，不用盒子。
6. **Emoji 当图标**——单一 SVG 图标族，统一线宽。
7. **只用颜色传达状态**——每个状态都要圆点 + 文字标签（a11y + 中英清晰）。
8. **弹窗优先**——详情视图用抽屉和行内展开，胜过弹窗；弹窗留给仪式（密钥展示）和破坏性确认。
9. **只有 spinner 还布局跳动**——用贴合内容形状的骨架行。
10. **移动端表格重排**——横向滚动 + 首列 sticky。
11. **等权重的卡片墙**——建立层级：每页一个 hero 数字/统计，其余信息安静（Sync 页的统计行就是模板）。
12. **浅色模式里随机出现深色区块**（反之亦然）——每套主题一套连续的色调系统。
13. **过饱和的强调色**——饱和度 <80%；强调色要往后退，不要尖叫（Linear："don't compete for attention you haven't earned"）。

---

## 7. 实现顺序（给后续 agent）

1. `porter/static/` 脚手架：`tokens.css`（§1 的精确值）、`app.css` 外壳 + 按钮/输入/徽章/卡片/表格、`panel.js`（约 60 行）、vendored `htmx.min.js`；在 app 里接 `StaticFiles`。
2. `panel.py`：`layout()` 外壳（§2 的侧边栏/header）+ `icon()` helper；7 个页面全部换上外壳。
3. 按流量顺序逐页构建：**Inbox → Memories → Sync → Clients → Audit → Snapshots → Settings**（Inbox 先——它是最高频页面，triage 卡片模式定下视觉基调）。
4. Toast 系统（`HX-Trigger`）+ 破坏性操作的 `hx-confirm` + 复制按钮。
5. header 里的主题开关 + 语言分段控件；响应式断点。
6. 全量测试绿 + 视觉截图走查（venv 里已有 Playwright，demo 时装过）+ 对比度抽查。

**验收线：** 任意页面和 Linear/Raycast/Supabase 截图摆在一起——应该像一家人。如果像 Bootstrap，就错了。
