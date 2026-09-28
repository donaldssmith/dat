# F4S_JOB v1

这份文档是当前用户脚本与部署台之间的共享任务协议。

目的只有一个：

1. 让脚本怎么发
2. 让部署台怎么收
3. 让后续迁移通知通道时不重写业务语义

也就是说：

1. Discord 不是协议
2. Telegram 也不是协议
3. 人类文案不是协议
4. `F4S_JOB` 才是协议

---

## 1. 当前状态

当前 `F4S_JOB` 版本为：

```json
{
  "version": 1,
  "protocol": "F4S_JOB"
}
```

只要：

1. `protocol !== "F4S_JOB"`
2. `version !== 1`

部署台就不应把它当作正式任务解析。

---

## 2. 外层封装

当前通知通道里，消息允许包含人类可读文案。

但真正给机器读取的部分，必须是一段 JSON。

推荐包裹方式：

```text
**F4S JOB**
任意给人看的说明
```json
{ ...job json... }
```
```

部署台当前只应解析：

1. 含有 `**F4S JOB**`
2. 且包含 ```json 代码块

其余旧消息直接视为历史垃圾，不做兼容要求。

---

## 3. 顶层字段

一个标准 `F4S_JOB v1` 对象如下：

```json
{
  "version": 1,
  "protocol": "F4S_JOB",
  "type": "full_upload",
  "source": "userscript-v13",
  "createdAt": "2026-04-21T20:00:00.000Z",
  "bookId": "712",
  "title": "Religion betrifft - AHS 7 -",
  "volume": "",
  "sourcePageCount": 116,
  "outline": {
    "status": "present",
    "count": 105
  },
  "pdf": {
    "mode": "manifest",
    "url": "https://example.com/712-pdf.parts.json",
    "label": "manifest: https://example.com/712-pdf.parts.json"
  },
  "meta": {
    "url": "https://example.com/712-0.dat"
  },
  "wanted": true,
  "reporter": {
    "installId": "f4s-abc12345",
    "name": "",
    "language": "zh-CN",
    "timezone": "Europe/Berlin"
  }
}
```

---

## 4. 字段定义

### 4.1 必填字段

以下字段必须存在：

1. `version`
   固定为 `1`
2. `protocol`
   固定为 `F4S_JOB`
3. `type`
   见后面的任务类型
4. `source`
   当前推荐写 `userscript-v13`
5. `createdAt`
   ISO 时间字符串
6. `bookId`
   教材编号，字符串
7. `title`
   教材标题，字符串

### 4.2 建议保留字段

以下字段当前建议保留：

1. `volume`
2. `sourcePageCount`
3. `outline`
4. `pdf`
5. `meta`
6. `wanted`
7. `reporter`

即使某个任务类型用不到，也尽量保留空对象或默认值，避免两端出现分支爆炸。

---

## 5. 任务类型

当前只允许四种 `type`。

### 5.1 `full_upload`

表示：

1. 用户已经上传了整本 PDF
2. 同时带上了 `0.dat`
3. 维护端可以正式处理整本补库

要求：

1. `pdf.mode` 必须是 `direct` 或 `manifest`
2. `pdf.url` 必须有效
3. `meta.url` 应存在

### 5.2 `meta_upload`

表示：

1. 云端 PDF 已有
2. 当前只需要补 `0.dat`

要求：

1. `pdf.mode` 固定为 `none`
2. `pdf.url` 为空
3. `meta.url` 必须有效

### 5.3 `nudge`

表示：

1. 用户声称自己已经完成上传
2. 现在只是提醒维护者尽快处理

要求：

1. 不要求 `pdf.url`
2. 不要求 `meta.url`
3. 不能自动当作正式补库任务

### 5.4 `inventory_signal`

表示：

1. 用户书架里发现这本书
2. 云端当前没有
3. 当前也不在 `-1.dat`
4. 这只是线索，不代表用户已经上传资产

要求：

1. `pdf.mode` 固定为 `none`
2. `meta.url` 为空
3. 部署台只展示，不自动入库

---

## 6. `pdf` 字段

`pdf` 统一为对象：

```json
{
  "mode": "direct",
  "url": "https://...",
  "label": "https://..."
}
```

### 允许值

1. `direct`
   表示 `url` 直接指向完整 PDF
2. `manifest`
   表示 `url` 指向分片清单 JSON
3. `none`
   表示当前任务没有 PDF 资产

### 当前部署台行为

1. `direct` -> 直接下载 PDF
2. `manifest` -> 读取清单，拼合 PDF
3. `none` -> 不尝试下载 PDF

---

## 7. `meta` 字段

`meta` 统一为对象：

```json
{
  "url": "https://..."
}
```

语义是：

1. 指向 `0.dat`

如果任务不包含元数据，可以为空字符串。

---

## 8. `outline` 字段

`outline` 统一为对象：

```json
{
  "status": "present",
  "count": 105
}
```

### `status` 当前允许值

1. `present`
2. `missing`
3. `unknown`
4. `no outline`

部署台当前只把它当展示字段，不用于决定是否能入库。

---

## 9. `reporter` 字段

`reporter` 只允许包含最小必要信息：

```json
{
  "installId": "f4s-abc12345",
  "name": "",
  "language": "zh-CN",
  "timezone": "Europe/Berlin"
}
```

用途：

1. 让维护者知道是不是同一个来源在反复上报
2. 帮助判断这本书是不是多人都能见到

明确不应包含：

1. 账号名
2. cookie
3. token
4. 浏览器指纹
5. 设备唯一标识
6. 任何可直接追踪真实身份的信息

---

## 10. 部署台处理规则

当前部署台对四类任务的标准动作是：

1. `full_upload`
   下载 PDF -> 切分 -> 下载 `0.dat` -> 部署
2. `meta_upload`
   只下载 `0.dat` -> 部署
3. `nudge`
   只展示 -> 不自动改仓库
4. `inventory_signal`
   只展示 -> 供维护者决定是否加入 `-1.dat`

---

## 11. 演进规则

如果以后协议升级：

1. 新版本必须改 `version`
2. 不允许偷偷改同版本字段语义
3. 通知通道可以换
4. 人类文案可以换
5. 但 `F4S_JOB` 的字段语义不能偷偷漂移

---

## 12. 当前一句话真相

当前系统里：

1. `F4S_JOB` 是脚本和部署台之间的机器协议
2. `Discord / Telegram` 只是运输车
3. `dist/<bookId>/` 才是最终正式真相
