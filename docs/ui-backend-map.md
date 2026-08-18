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

## 运行模式

`GET /v1/status` 与 `GET /v1/modes` 直接读取当前焦点应用、tree interceptor、输入管理器、语音合成器、盲文 handler、配置文件和屏幕幕布状态。`PATCH /v1/modes` 只允许 `inputHelp`、当前应用 `sleepMode` 和当前文档 `browseMode`，复用 NVDA 原生属性及焦点事件顺序；屏幕幕布保持只读，避免绕过其警告流程。

## 文本范围

文本接口通过对象的 `makeTextInfo` 和 NVDA `UNIT_CHARACTER` 移动范围，不序列化各提供者不兼容的原生 bookmark。读取受字符数与偏移上限约束；写入同时校验对象 generation 与基于当前文本范围签名的 `baseRevision`，再调用 `updateCaret()` 或 `updateSelection()`。

## 诊断清单

add-on、global plugin、语音合成器和盲文驱动清单直接来自 NVDA 已加载的 handler。同步端点只读；异步诊断任务在专用目录生成限额 ZIP，只包含结构化清单和有界日志尾部，不接受调用者指定的服务端文件路径。

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

- 四类资源都不要求凭据，并继承 Host/Origin、回环地址、安全桌面、请求体和并发限制。任何本机进程都能调用 Bridge。
- 排队超时保证任务未执行，`completionUnknown=false`；已经开始的主线程调用超时返回 `504 mainThreadTimeout` 和 `completionUnknown=true`。
- 客户端遇到未知完成状态后只 GET 同一资源对账，不自动重发写请求。
- 文件保存异常时恢复内存快照并尝试恢复磁盘；因为 NVDA 保存过程未统一承诺事务原子性，接口返回 `partialFailure` 和回滚信息。
- 除树导出、备份、诊断导出和专用重启外，配置与文本操作保持同步；任何接口都不调用 `ShowModal` 或消息框。重启继续使用独立生命周期端点，绝不通过通用 gesture。
