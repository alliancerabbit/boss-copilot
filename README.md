# BossCopilot

基于 Chrome Side Panel + 本地 DeepSeek 的 Boss 直聘人机协同求职助手：粘贴岗位 JD，得到匹配度、短板、打招呼语与面试模拟题。

## 快速开始

### 1. 启动后端

```bash
cd backend
python -m venv .venv                                          # 首次
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env                                        # 填入 DEEPSEEK_API_KEY
.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

macOS / Linux 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`，`copy` 换成 `cp`。

### 2. 加载插件

打开 `chrome://extensions` → 右上角开启「开发者模式」→「加载已解压的扩展程序」→ 选择本仓库的 `extension/` 目录。

### 3. 配置简历（不用改任何文件）

点击工具栏的 BossCopilot 图标打开侧边栏 → 展开「我的简历」：
直接粘贴简历文本，或上传 `.pdf` / `.md` / `.txt`（PDF 由后端用 pypdf 提取文字）→ 点「保存简历」。

保存后写入本地 `backend/resume.md`。该文件已被 `.gitignore` 忽略，不会进入仓库。

### 4. 使用

打开 Boss 直聘岗位详情页 → 点击工具栏图标 →「分析当前岗位」或「生成面试题」。
若抓不到 JD，先在页面上用鼠标选中 JD 文本，再点一次按钮。

## 自带 Key（BYOK）与免费模型

**支持智谱 `glm-4-flash`、硅基流动等免费 API，开箱即用**：侧边栏顶部「API 设置」里切换服务商即可，不用改任何配置文件。

| 预设 | Base URL | 默认模型 | 说明 |
|---|---|---|---|
| 智谱清言 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 有免费额度，推荐先用这个 |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` | 免费开源模型 |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | 默认，需账户余额 |
| 豆包 / 火山引擎 | `https://ark.cn-beijing.volces.com/api/v3` | 填接入点 ID（`ep-` 开头） | 注意不是模型名 |
| 自定义 | 任意 OpenAI 兼容地址 | 任意 | — |

- Key 只存在本机浏览器的 `chrome.storage.local`，不上传、不入库。
- 不填 Key 时，后端自动回落到 `backend/.env` 的默认配置。
- 配置通过 `X-API-Key` / `X-Base-URL` / `X-Model-Name` 三个请求头传给后端；「测试连接」会发一句 `hi` 验证 Key、地址与模型名是否可用。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/analyze` | JD + 简历 → 匹配度、3 条优势、2-3 条短板、3 条打招呼语 |
| POST | `/api/interview` | JD + 简历 → 8 道面试题（3 项目深挖 / 3 短板追问 / 2 场景设计） |
| POST | `/api/test-connection` | 用请求头里的配置发一句 `hi` 探活 |
| GET | `/api/resume` | 读取当前简历 |
| POST | `/api/resume` | 保存粘贴或微调后的简历 |
| POST | `/api/resume/upload` | 上传 `.pdf` / `.md` / `.txt` 并解析写入 |
| GET | `/api/health` | 自检：简历是否存在、密钥是否已配 |

## 市场趋势分析

```bash
.venv\Scripts\python.exe backend\market_report.py
```

读取 `backend/data/jobs.jsonl` 里积累的历史岗位，生成 `backend/data/market_report.md`：高频技能 Top 10、简历缺口、针对性修改建议。
`backend/data/` 与 `backend/resume.md` 均为本地私有数据，不入库。
