# Hugging Face 部署说明

这份只写当前落地要做的事，不讲后厨理论。

## 1. 现在的结构

现在系统是双轨：

1. Hugging Face
   主分发、主发布
2. GitHub
   保留为后备，但默认被阻断

也就是说：

1. 用户脚本先读 Hugging Face
2. 读不到再退 GitHub
3. 部署台默认只往 Hugging Face 发

## 2. 你要准备什么

只要三样：

1. 一个公开的 Hugging Face dataset 仓库
   例如：`donaldssmith/dat`
2. 一个有 `write` 权限的 Hugging Face token
3. 本地部署配置文件
   文件：`config/local/部署台.deploy.json`

## 3. 一次性配置

### 3.1 建仓库

去 Hugging Face 新建一个公开 dataset 仓库。

仓库名建议直接和现在保持一致：

```text
donaldssmith/dat
```

### 3.2 配 token

可以二选一：

1. 推荐：设环境变量 `HF_TOKEN`
2. 备选：直接填进 `config/local/部署台.deploy.json` 的 `huggingface.token`

### 3.3 检查部署配置

当前默认配置已经是：

1. `huggingface` 为主目标
2. `github` 仍保留
3. `github.blocked = true`

如果哪天要恢复 GitHub，只改这个文件里的 `blocked` 即可。

## 4. 日常怎么用

和平时一样跑部署台。

当它进入“准备部署当前仓库”时：

1. 默认会显示主目标是 Hugging Face
2. 下面会显示 GitHub 仍在，但被阻断
3. 继续确认即可

## 5. 现在脚本怎么读文件

用户脚本当前读取顺序是：

1. `https://huggingface.co/datasets/<repo>/resolve/main/dist/...`
2. `https://cdn.jsdelivr.net/gh/...`
3. `https://raw.githubusercontent.com/...`

所以迁移时不会一下把 GitHub 切死。

## 6. 如果 Hugging Face 抽风

当前不会自动切回 GitHub 发布。

这是故意的。

因为现在的设计是：

1. GitHub 只是后备基础设施
2. 平时不应该被误触发

如果真要恢复 GitHub 发布：

1. 打开 `config/local/部署台.deploy.json`
2. 把 `github.blocked` 改成 `false`
3. 视需要把 `primary_target` 改成 `github`

就够了。
