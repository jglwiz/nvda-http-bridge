# NVDA HTTP Bridge

[English](README.md) | 简体中文

## 项目初衷

NVDA 拥有丰富的内部接口，但这些能力通常只能在 NVDA 进程内由插件调用。**NVDA HTTP Bridge 的目标，是把其中适合自动化的能力转换为受约束、版本化的本机 HTTP API，提供给 Codex、Claude 等编程工具使用。**

借助这座桥梁，编程 agent 可以在明确的安全边界内读取 NVDA 的运行状态、焦点与无障碍对象、语音和日志，协助排查内部问题；也可以查询或修改受支持的配置，并在 NVDA 插件开发过程中完成诊断、验证与回归测试。

本项目并不是远程控制服务，也不会暴露任意 Python 执行、模块导入或通用文件访问能力。它只监听本机回环地址，所有接口都经过明确的 schema、资源上限和安全桌面检查。

## 项目简介

配置 API 的 NVDA 前端/后端对应关系见 [docs/ui-backend-map.md](docs/ui-backend-map.md)。这些接口调用 NVDA 自身配置对象与保存流程，不执行 GUI 模拟。

NVDA HTTP Bridge 是一个仅监听本机回环地址的 NVDA 全局插件。它为 CLI、自动化测试和 agent 提供版本化的焦点、对象、语音、事件、树查询和受控动作 API。

## 命名

- 项目名称使用 **NVDA HTTP Bridge**，仓库目录使用 `nvda-http-bridge`。
- `NVDA CLI` 仅适合描述本仓库中的客户端，不足以涵盖 NVDA 插件、HTTP API 和 Codex skill，因此不作为项目名。
- NVDA add-on ID、源码文件、实现包和构建产物统一使用 `nvdaHttpBridge`。

## 仓库结构

```text
nvda-addon/  NVDA 全局插件源码与 manifest
skill/       Codex skill 与安全 CLI 客户端
tests/       HTTP Bridge 单元测试
release/     本地发布产物（不纳入 Git）
build.ps1    NVDA add-on 打包脚本
```

## 安全与性能原则

- 只监听 `127.0.0.1:19281`，拒绝非回环 `Host`。
- 不使用 token 或其他客户端身份验证；任何本机进程都能调用接口，因此不要把端口代理、转发或暴露给其他主机。
- 普通树查询默认限制为深度 3、每个父节点 20 个子节点、总计 200 个节点和 500 ms 软时间预算；同步 JSON 结果另有 2 MiB 总预算。
- 同步查询最多允许 1000 个节点和 3 秒；遍历仍以最多 25 个节点或约 20 ms 的主线程切片执行，更大的请求必须使用异步导出。
- 完整树导出按批次读取 NVDAObject，并以 NDJSON 写入临时文件，不在内存中构造完整树。
- UIA/IA2 的单个属性调用无法安全中断；时间预算会在调用前和批次之间检查，但不是硬实时保证。
- Windows 锁屏或进入安全桌面时，插件拒绝数据和动作请求，清空语音/事件/对象缓存，并取消树、诊断与备份任务。
- 文本读取使用字符数、偏移和对象 generation 硬上限；插入点/选择写入同时要求最新 revision，不暴露提供者原生 bookmark。
- 插件、全局插件和驱动清单只读；诊断导出只生成受限 ZIP，不接受调用者提供的服务端路径。
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

产物写入 `release/nvdaHttpBridge-0.1.0.nvda-addon`，构建脚本会排除 `__pycache__` 和 `.pyc`。

所有接口都不要求凭据。访问边界仅由 `127.0.0.1` 监听、Host/浏览器来源检查、安全桌面限制、严格 schema 与资源限制构成。旧版本遗留的 `%APPDATA%\nvda\nvdaHttpBridge.token` 不再读取或创建。

## 基础接口

```powershell
curl.exe http://127.0.0.1:19281/health
curl.exe http://127.0.0.1:19281/v1/version
curl.exe http://127.0.0.1:19281/v1/capabilities
curl.exe http://127.0.0.1:19281/v1/status
curl.exe http://127.0.0.1:19281/v1/objects/focus
```

`/v1/capabilities` 是默认限制、同步硬上限、字段、动作和事件类型的权威来源。

## 运行状态与模式

```text
GET   /v1/status
GET   /v1/modes
PATCH /v1/modes
```

`status` 汇总当前配置文件、应用、语音合成器、盲文显示器和模式。`modes` 为每个字段声明 `available`/`writable`；当前只允许修改 `inputHelp`、当前应用的 `sleepMode` 和当前文档的 `browseMode`，屏幕幕布只读。PATCH 必须携带最新 `baseRevision`，这些会话模式不会由端点持久化。

## 有界文本接口

```text
GET  /v1/text/caret?maxChars=4096
GET  /v1/text/selection?maxChars=4096
GET  /v1/text/object/{objectId}?offset=0&maxChars=4096
POST /v1/actions/set-caret
POST /v1/actions/set-selection
```

对象文本可按 NVDA 字符移动单位分页，默认 4096、单次最多 32768 字符，起点偏移最多 100000。写操作要求同一次读取返回的 `objectId`、`generation` 和 `revision`；插入点使用 `offset`，选择使用 `start`/`end`。焦点、文档或文本变化后返回 `409 staleObject`/`staleState`，客户端必须重新读取，不能自动重试。若当前 NVDA 对象不提供请求的插入点或选择文本位置，读取返回 `409 textPositionUnavailable`，而不是内部服务器错误。

## 只读清单与诊断导出

```text
GET /v1/addons
GET /v1/global-plugins
GET /v1/drivers
GET /v1/diagnostics

POST   /v1/diagnostics/exports
GET    /v1/diagnostics/exports/{jobId}
GET    /v1/diagnostics/exports/{jobId}/data
DELETE /v1/diagnostics/exports/{jobId}
```

同步清单不修改 NVDA。诊断任务只接受 `{}`，生成最多 5 MiB 的 ZIP，包含结构化清单和最多 2 MiB 的 NVDA 日志尾部；最多同时运行 1 个、保留 4 个。安全上下文变化会立即撤销下载并清理临时文件。

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

配置资源无需身份验证，并在 NVDA 主线程中通过专用 adapter 调用其后端：

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

超过同步硬上限时，接口返回 `422 exportRequired`。创建导出任务无需凭据：

```powershell
$body = @{
  root = "foreground"
  depth = $null
  maxChildren = $null
  maxNodes = $null
  include = @("name", "role", "states", "className", "appName")
  format = "flat"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:19281/v1/tree/exports `
  -ContentType application/json -Body $body
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

备份任务调用 NVDA 自身的便携版创建实现，并包含当前用户配置。所有接口均无需凭据：

```text
POST   /v1/backups
GET    /v1/backups/{jobId}
DELETE /v1/backups/{jobId}
```

`POST` 只接受 `{"targetPath":"D:\\backups"}`，并在目标文件夹中创建新的 `nvda` 子文件夹；目标文件夹可以已存在，也可由插件创建。若 `D:\\backups\\nvda` 已存在则拒绝覆盖。状态响应使用 `backupPath` 返回实际目录。删除或过期 HTTP 任务不会删除已完成的备份；为避免复制旧版凭据，备份仍会排除遗留的 `nvdaHttpBridge.token` 文件。

## 事件流

SSE 事件流包含焦点、前台、名称、值、状态、插入点和语音事件。它属于敏感读取，但不要求凭据。

```powershell
curl.exe -N http://127.0.0.1:19281/v1/events
```

可使用 `?types=gainFocus,speech` 过滤；断线重连可以发送 `Last-Event-ID`。插件重载或缓冲溢出时会发送 `reset` 事件。

## 动作

所有动作都无需凭据：

```text
POST /v1/actions/speak
POST /v1/actions/cancel-speech
POST /v1/actions/gesture
POST /v1/actions/focus
POST /v1/actions/default-action
POST /v1/actions/set-caret
POST /v1/actions/set-selection
```

示例：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:19281/v1/actions/speak `
  -ContentType application/json -Body '{"text":"任务完成"}'
```

通用 action dispatcher 继续拒绝 `restart` 以及会退出、重启或重载插件的 gesture。进程重启只由下面的专用生命周期端点提供。

`gesture` 接受普通键名以及 `GET /v1/gestures` 返回的 `kb:` / `kb(layout):` 标识；Bridge 会把规范化排序还原为 NVDA 构造手势所需的“主键在最后”顺序。命令按 NVDA 执行该请求时的当前焦点上下文解析，不会把未处理的按键透传给前台应用。语法有效但在当前上下文中没有绑定的手势返回 `409 gestureNotBound`；客户端应重新读取焦点和 `GET /v1/gestures`，不得自动重试该动作。

## NVDA 进程重启

`POST /v1/lifecycle/restart` 无需凭据，请求体必须为 `{}`。服务端在锁屏、安全桌面或 NVDA 模态消息框活动时拒绝请求；接受请求后先完整返回 `202 Accepted` 并关闭请求连接，再把 NVDA 原生 `core.restart()` 投递到主线程。

原始 HTTP 的 `202` 只表示已接受，不表示重启完成。推荐使用同步封装：

```powershell
python skill/scripts/nvda_http_bridge.py restart --wait-seconds 30
```

CLI 先读取 live capabilities；若声明专用端点，只发送一次 POST，传输中断后也不会重发或降级为快捷键。随后轮询 `/health`，只有 `nvdaProcessId` 或 `nvdaStartTime` 与重启前不同才报告成功；`uptimeMs` 降低仅作辅助证据。只有旧 Bridge 未声明专用能力时，CLI 才使用 `--nvda-key insert|capslock` 的外部 `NVDA+Shift+Q` 兼容回退。首次从旧版升级仍需要一次 bootstrap restart 才能激活新端点。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m compileall nvda-addon/globalPlugins
```

实机验证至少覆盖 NVDA 2025.3.3 和 2026.1.1，以及 Win32、UIA、Chromium/IA2、锁屏、安全桌面、插件连续重载、导出取消、完整备份和对象失效场景。

## 主线程超时排查

如果 `/health` 仍能响应，但对象、树和 `cancel-speech` 都返回 `504 mainThreadTimeout`，应先查看 NVDA 日志中的主线程冻结栈。若栈停在 `winAPI.sessionTracking` 调用 `WTSCurrentSessionInfoEx`，问题发生在 NVDA/Windows 会话状态查询，不是树遍历或 HTTP JSON 编码。

在本次验证主机上，`TermService` 为停止状态时启动 NVDA 会触发该冻结；临时启动 Remote Desktop Services 后再启动 NVDA即可完成 WTS 初始化。初始化成功后把服务恢复为原来的停止/Manual 状态，NVDA 仍持续可用。这个结论是针对该主机的故障排查结果，不是插件的运行依赖；操作 Windows 服务需要管理员权限，也不应擅自改变服务启动类型。
