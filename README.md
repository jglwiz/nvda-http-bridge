# NVDA HTTP Bridge

配置 API 的 NVDA 前端/后端对应关系见 [docs/ui-backend-map.md](docs/ui-backend-map.md)。这些接口调用 NVDA 自身配置对象与保存流程，不执行 GUI 模拟。

NVDA HTTP Bridge 是一个仅监听本机回环地址的 NVDA 全局插件。它为 CLI、自动化测试和 agent 提供版本化的焦点、对象、语音、事件、树查询和受控动作 API。

## 命名

- 项目名称使用 **NVDA HTTP Bridge**，仓库目录使用 `nvda-http-bridge`。
- `NVDA CLI` 仅适合描述本仓库中的客户端，不足以涵盖 NVDA 插件、HTTP API 和 Codex skill，因此不作为项目名。
- NVDA add-on ID、源码文件、实现包和构建产物统一使用 `nvdaHttpBridge`。
- token 文件名固定为 `nvdaHttpBridge.token`。

## 仓库结构

```text
nvda-addon/  NVDA 全局插件源码与 manifest
skill/       Codex skill 与安全 CLI 客户端
tests/       HTTP Bridge 单元测试
dist/        本地构建产物（不纳入 Git）
build.ps1    NVDA add-on 打包脚本
```

## 安全与性能原则

- 只监听 `127.0.0.1:19281`，拒绝非回环 `Host`。
- 普通树查询默认限制为深度 3、每个父节点 20 个子节点、总计 200 个节点和 500 ms 软时间预算；同步 JSON 结果另有 2 MiB 总预算。
- 同步查询最多允许 1000 个节点和 3 秒；遍历仍以最多 25 个节点或约 20 ms 的主线程切片执行，更大的请求必须使用异步导出。
- 完整树导出按批次读取 NVDAObject，并以 NDJSON 写入临时文件，不在内存中构造完整树。
- UIA/IA2 的单个属性调用无法安全中断；时间预算会在调用前和批次之间检查，但不是硬实时保证。
- Windows 锁屏或进入安全桌面时，插件拒绝数据和动作请求，清空语音/事件/对象缓存，并取消导出与备份。
- 完整备份由插件异步直写到调用者指定目标中的全新 `nvda` 子目录，不生成中转 ZIP，也不提供任意精确输出路径。
- 插件不提供 `eval`、任意 Python 调用、任意模块导入或通用文件访问。
- 浏览器跨站 `Origin` / `Sec-Fetch-Site` 请求会在进入 NVDA 主线程前被拒绝。

## 安装

开发调试时，将以下内容复制到 NVDA scratchpad 的 `globalPlugins` 目录：

```text
nvdaHttpBridge.py
_nvdaHttpBridge/
```

然后在 NVDA 高级设置中启用 Developer Scratchpad，并重启 NVDA 或手动重载插件。

正式打包时使用 `nvda-addon/manifest.ini` 与 `nvda-addon/globalPlugins/`。

```powershell
.\build.ps1
```

产物写入 `dist/nvdaHttpBridge-1.1.1.nvda-addon`，构建脚本会排除 `__pycache__` 和 `.pyc`。

启动后，写操作 token 位于：

```text
%APPDATA%\nvda\nvdaHttpBridge.token
```

token 必须通过以下任一请求头发送，禁止放入 URL。写操作、语音历史、日志、事件流和所有导出接口始终要求 token；普通对象与有界树读取是否要求 token 可由插件配置控制，当前默认仅限回环访问。

```text
Authorization: Bearer <token>
X-NVDA-HTTP-Token: <token>
```

## 基础接口

```powershell
curl.exe http://127.0.0.1:19281/health
curl.exe http://127.0.0.1:19281/v1/version
curl.exe http://127.0.0.1:19281/v1/capabilities
curl.exe http://127.0.0.1:19281/v1/objects/focus
```

`/v1/capabilities` 是默认限制、同步硬上限、字段、动作和事件类型的权威来源。

## 有界树查询

默认查询：

```powershell
curl.exe "http://127.0.0.1:19281/v1/tree?root=focus"
```

显式扩大范围并只读取指定字段：

```powershell
curl.exe "http://127.0.0.1:19281/v1/tree?root=foreground&depth=6&maxChildren=100&maxNodes=800&timeoutMs=2500&format=flat&include=name,role,states,className"
```

响应包含：

- `generation`
- 实际使用的 `limits`
- `nodeCount` 与 `elapsedMs`
- `truncated` 与 `truncationReasons`
- `tree`

可能的截断原因包括 `depthLimit`、`childLimit`、`nodeLimit`、`timeLimit`、`sizeLimit` 和 `cycleDetected`。

## 同步配置接口

配置资源始终要求 token，并在 NVDA 主线程中通过专用 adapter 调用其后端：

```text
GET   /v1/settings/categories
GET   /v1/settings/general
PATCH /v1/settings/general

GET  /v1/speech-dictionaries
GET  /v1/speech-dictionaries/{default|voice|temp}
POST /v1/speech-dictionaries/{id}/validate
PUT  /v1/speech-dictionaries/{id}

GET /v1/symbol-dictionaries/{locale|current}
PUT /v1/symbol-dictionaries/{locale}

GET   /v1/gestures?context=current&filter=...
PATCH /v1/gestures
```

写请求必须带最新 GET 返回的 `baseRevision`。General 另带 `values`；朗读词典 PUT 带完整 `entries`；符号 PUT 带 `updates`/`remove`；手势 PATCH 带 `operations`。General 不自动保存全部配置；语言变化只报告需要重启。非空朗读词典整体清空和手势全部重置暂不支持，因为对应 UI 操作要求确认。

CLI 使用 JSON 文件承载结构化写请求，例如：

```powershell
python skill/scripts/nvda_http_bridge.py settings-get
python skill/scripts/nvda_http_bridge.py settings-set --body-file settings-change.json
python skill/scripts/nvda_http_bridge.py speech-dictionary-get default
python skill/scripts/nvda_http_bridge.py symbols-get current
python skill/scripts/nvda_http_bridge.py gestures-get --filter time
```

如果写请求在 NVDA 主线程已经开始后发生超时，响应包含 `completionUnknown=true`；CLI 会自动 GET 对应资源并把实际状态放入 `reconciliation`，不会自动重发写请求。

## 异步完整树导出

超过同步硬上限时，接口返回 `422 exportRequired`。创建导出任务需要 token：

```powershell
$token = Get-Content "$env:APPDATA\nvda\nvdaHttpBridge.token" -Raw
$headers = @{ Authorization = "Bearer $($token.Trim())" }
$body = @{
  root = "foreground"
  depth = $null
  maxChildren = $null
  maxNodes = $null
  include = @("name", "role", "states", "className", "appName")
  format = "flat"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:19281/v1/tree/exports `
  -Headers $headers -ContentType application/json -Body $body
```

任务接口：

```text
POST   /v1/tree/exports
GET    /v1/tree/exports/{jobId}
GET    /v1/tree/exports/{jobId}/data
DELETE /v1/tree/exports/{jobId}
```

`null` 表示不设置用户级限制，但循环检测、紧急节点/深度/子项上限、文件配额、最长运行时间和 TTL 仍然生效。单任务最多 100 MiB，全部保留结果合计最多 200 MiB、最多 8 个；创建频率限制为每分钟 10 次。完成文件保留到 TTL 或显式 `DELETE`，下载不会立即删除文件。

## 异步完整 NVDA 备份

备份任务调用 NVDA 自身的便携版创建实现，并包含当前用户配置。所有接口都要求 token：

```text
POST   /v1/backups
GET    /v1/backups/{jobId}
DELETE /v1/backups/{jobId}
```

`POST` 只接受 `{"targetPath":"D:\\backups"}`，并在目标文件夹中创建新的 `nvda` 子文件夹；目标文件夹可以已存在，也可由插件创建。若 `D:\\backups\\nvda` 已存在则拒绝覆盖。状态响应使用 `backupPath` 返回实际目录。删除或过期 HTTP 任务不会删除已完成的备份，备份中会移除 HTTP token 文件。

## 事件流

SSE 事件流包含焦点、前台、名称、值、状态、插入点和语音事件。事件流属于敏感读取，始终要求 token。

```powershell
curl.exe -N http://127.0.0.1:19281/v1/events `
  -H "Authorization: Bearer $($token.Trim())"
```

可使用 `?types=gainFocus,speech` 过滤；断线重连可以发送 `Last-Event-ID`。插件重载或缓冲溢出时会发送 `reset` 事件。

## 动作

所有动作都要求 token：

```text
POST /v1/actions/speak
POST /v1/actions/cancel-speech
POST /v1/actions/gesture
POST /v1/actions/focus
POST /v1/actions/default-action
```

示例：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:19281/v1/actions/speak `
  -Headers $headers -ContentType application/json -Body '{"text":"任务完成"}'
```

HTTP 请求过程中触发插件重载可能造成生命周期互等，因此 `restart` 动作已明确禁用。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m compileall nvda-addon/globalPlugins
```

实机验证至少覆盖 NVDA 2025.3.3，以及 Win32、UIA、Chromium/IA2、锁屏、安全桌面、插件连续重载、导出取消、完整备份和对象失效场景。

## 主线程超时排查

如果 `/health` 仍能响应，但对象、树和 `cancel-speech` 都返回 `504 mainThreadTimeout`，应先查看 NVDA 日志中的主线程冻结栈。若栈停在 `winAPI.sessionTracking` 调用 `WTSCurrentSessionInfoEx`，问题发生在 NVDA/Windows 会话状态查询，不是树遍历或 HTTP JSON 编码。

在本次验证主机上，`TermService` 为停止状态时启动 NVDA 会触发该冻结；临时启动 Remote Desktop Services 后再启动 NVDA即可完成 WTS 初始化。初始化成功后把服务恢复为原来的停止/Manual 状态，NVDA 仍持续可用。这个结论是针对该主机的故障排查结果，不是插件的运行依赖；操作 Windows 服务需要管理员权限，也不应擅自改变服务启动类型。
