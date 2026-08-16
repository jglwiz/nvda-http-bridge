# NVDA UI 到配置 API 的后端映射

本文记录配置 API 的实现依据。HTTP Bridge 不模拟鼠标、键盘或对话框操作；它复用 NVDA 前端在“确定/应用”时调用的同一后端对象和保存流程。

## General 设置

| 项目 | NVDA UI 依据 | API 后端 | 持久化与副作用 |
|---|---|---|---|
| 界面语言 | `GeneralSettingsPanel.makeSettings/onSave/postSave` | `languageHandler.getAvailableLanguages/isLanguageForced`、`config.conf["general"]["language"]` | 仅修改当前配置；不调用 `config.conf.save()`；变化时返回 `restartRequired=true`，不弹窗、不重启 |
| 退出时保存 | `GeneralSettingsPanel.onSave` | `config.conf["general"]["saveConfigurationOnExit"]` | 当前配置，`persisted=false` |
| 退出选项 | 同上 | `config.conf["general"]["askToExit"]` | 当前配置，`persisted=false` |
| 启停声音 | 同上 | `config.conf["general"]["playStartAndExitSounds"]` | 当前配置，`persisted=false` |
| 阻止显示器关闭 | 同上 | `config.conf["general"]["preventDisplayTurningOff"]` | 当前配置，`persisted=false` |

`PATCH /v1/settings/general` 使用完整写前快照和 `baseRevision`。所有字段先验证后写入；语言被命令行强制时只读。登录启动、更新、日志级别、驱动和“保存全部配置”不在本阶段。

## 朗读词典

UI 依据是 `gui/speechDict.py` 的 `DictionaryDialog.onOk` 和词条编辑校验。API 使用运行版本提供的 `SpeechDictEntry`、default/voice/temp `SpeechDict` 以及词典自己的 `save()`：

- default 和 voice 使用 NVDA 选择的实际文件；API 不接受文件路径。
- temporary 词典只在本次会话有效，返回 `persisted=false`。
- 主线 NVDA 的 `speechDictHandler.types/definitions` 与 NVDA 2025.3.3 的 `speechDictHandler` 差异只封装在 speech dictionary adapter 中。
- 构造每个 `SpeechDictEntry` 并执行一次替换，以同时校验模式、正则和替换分组。
- UI 的“全部删除”需要确认，所以非空词典不能通过空 `entries` 绕过确认。

## 标点与符号读音

UI 依据是 `SpeechSymbolsDialog.onOk`。提交顺序与 UI 一致：

1. 使用 `SpeechSymbolProcessor.deleteSymbol/updateSymbol` 更新用户覆盖。
2. 调用 `processor.userSymbols.save()`。
3. 使 `SpeechSymbolProcessor.localeSymbols` 和 `_localeSpeechSymbolProcessors` 的对应 locale 缓存失效。

API 只接受 locale 和结构化符号字段，不接受 `symbols-*.dic` 路径。删除只允许已有用户覆盖；纯内置定义不能删除。level 映射为 `none/some/most/all/character`，preserve 映射为 `never/always/belowLevel`。

## 按键与输入手势

UI 依据是 `gui/inputGestures.py` 的 `_getAllGestureScriptInfo` 与 `_InputGesturesViewModel.commitChanges`。Web 请求没有打开设置对话框，因此直接以当前焦点对象和祖先调用 `inputCore.manager.getAllGestureMappings`，并使用 `userGestureMap`：

- `add`：移除同类 `None` 覆盖后增加用户绑定。
- `remove`：只移除确实存在的用户绑定。
- `unbind`：为目标 class 增加 `script=None`，等价于 UI 解除继承绑定。
- `addKbEmulation`：以 `globalCommands.GlobalCommands` 和规范化 `kb:*` 脚本建立系统键盘模拟。
- 所有 gesture identifier 先通过 `normalizeGestureIdentifier` 和 NVDA 的显示文本解析验证；所有变更后只调用一次 `userGestureMap.save()`。

revision 包含焦点 UI context generation 和用户手势映射。焦点上下文变化会产生 `409 staleState`。“捕获按键”和“全部恢复出厂默认”不对 Web 开放。

## 共同执行和错误语义

```text
HTTP route → BridgeService → 专用 adapter → MainThreadExecutor → NVDA 后端
```

- 四类资源都要求本地 token，并继承 Host/Origin、回环地址、安全桌面、请求体和并发限制。
- 排队超时保证任务未执行，`completionUnknown=false`；已经开始的主线程调用超时返回 `504 mainThreadTimeout` 和 `completionUnknown=true`。
- 客户端遇到未知完成状态后只 GET 同一资源对账，不自动重发写请求。
- 文件保存异常时恢复内存快照并尝试恢复磁盘；因为 NVDA 保存过程未统一承诺事务原子性，接口返回 `partialFailure` 和回滚信息。
- 本阶段没有异步 operation、202、GUI handler、`ShowModal`、消息框或 NVDA 重启接口。
