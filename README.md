# Fool4School

Fool4School 是 Digi4School 的本地 PDF 处理、书库发布和补库任务项目。

## 从哪里开始

项目现在只有一个总入口：

```powershell
python run.py --help
```

常用流程：

```powershell
# 启动后端任务接收端
python run.py backend

# 处理队列中的一条 F4S_JOB
python run.py worker --once

# 从 Discord 导入请求并处理队列中的一条任务
python run.py worker --discord-inbox --once

# 持续轮询 Discord 并处理新请求
python run.py worker --watch-discord

# 将失败任务重新入队后继续处理
python run.py worker --retry-failed

# 整理 Discord 历史任务（按配置决定是否删除）
python run.py worker --discord-cleanup

# 继续使用旧部署台的切分/发布参数
python run.py deploy --help

# 打开本地书库合成器
python run.py library
```

首次使用切分或本地合成器时安装 PDF 依赖：

```powershell
python -m pip install -r requirements.txt
```

项目版本由 `pyproject.toml` 管理，提交到 `main` 后由 GitHub Actions 自动执行编译和测试。

## 目录职责

| 目录 | 用途 |
| --- | --- |
| `app/` | F4S_JOB 后端、教材工具和当前用户脚本 |
| `data/source/` | ABBYY Hot Folder 输入区 |
| `data/ocr/` | OCR 后的 PDF 成品区 |
| `dist/` | 正式书库切片：`0.dat` 元数据、`1.dat ... N.dat` 页面 |
| `data/exports/` | 本地合成 PDF 的输出目录 |
| `docs/research/` | 逆向研究结论和交接文档；原始抓取文件不纳入版本库 |
| `docs/archive/` | 历史脚本版本 |
| `docs/` | 协议和系统设计说明 |

## 后端接口

启动后默认监听 `http://127.0.0.1:8787`：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
Invoke-RestMethod http://127.0.0.1:8787/api/jobs
```

向 `POST /api/jobs` 提交完整的 `F4S_JOB v1` JSON 后，任务会写入 `.f4s/jobs.json`。worker 负责领取任务、处理 PDF/元数据并写回 `succeeded` 或 `failed` 状态。相同消息 ID 会幂等去重。

`--discord-inbox` 使用 `config/local/部署台.bot.json` 读取 Bot Token 和频道 ID，拉取频道中的 `F4S JOB` 代码块，校验通过后写入 `.f4s/jobs.json`。相同 Discord 消息 ID 只会入队一次。需要常驻运行时使用 `--watch-discord`，它默认每 30 秒轮询一次。worker 处理成功后会记录来源消息为已处理；将 `delete_processed_messages` 设为 `true` 时才会删除 Discord 原消息。失败任务保持 `failed`，使用 `--retry-failed` 后才会重新入队。

## 配置

运行时状态放在 `.f4s/`，不要提交到仓库。Discord、发布目标等本地配置位于 `config/local/`；可复制 `config/bot.example.json` 和 `config/deploy.example.json` 作为模板。

当前前端脚本在 `app/userscripts/V14.user.js`。它默认继续使用 Discord 通知；设置脚本中的 `BACKEND_JOB_URL` 后，可以直接把结构化任务 POST 到新后端。
