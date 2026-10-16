# PSP ISO 到 PC / Android 高清重制包 Skill

可供 Codex 调用的工作流程：将用户提供的 PSP ISO 或已有原生工程，逐步做成 Windows PC 或 Android 高清试玩包，完成鼠标/触控适配、高清资源、无损瘦身及分发验证。

技能名：`psp-iso-to-pc-remaster`。

## 覆盖范围

1. 镜像获取、身份与格式检查，记录来源、版本和 SHA-256。
2. 原生重编译、运行时兼容修复和分阶段游玩验证。
3. 鼠标操作，以及按游戏情况接入 AUTO / SKIP。
4. 背景、角色图集、字库、UI 与动态表情的高清处理。
5. ISO 资源去重、PNG / WebP 无损存储和加载性能检查。
6. 原画质与高清分支、相对路径启动器、依赖收集、便携 ZIP 和完整性验证。
7. Android ARM64 / Java-JNI / GLES / AAudio 适配、横屏触控和后台恢复，完整 APK、更新小包及高清资源 ZIP。
8. 安卓资源流式安装与失败恢复、同签名覆盖升级保留存档、16 KiB 对齐检查，以及离屏测试和真机验证分层。

它是一套工程方法与辅助工具，不是一键转换任意游戏的程序。新游戏需要重新验证主程序、资源格式、运行时兼容性及游戏特定地址。

## 安装到 Codex

将本仓库根目录放到 Codex 技能目录下，文件夹名称使用 `psp-iso-to-pc-remaster`。默认目录为 `~/.codex/skills`；配置了 `CODEX_HOME` 时使用其下的 `skills`。

Windows PowerShell 示例：

```powershell
$skillRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex/skills' }
git clone https://github.com/gangsterkino/-psp-iso-to-pc-remaster.git (Join-Path $skillRoot 'psp-iso-to-pc-remaster')
```

如果目标目录已存在，先检查并保留已有修改，不要直接覆盖。安装后在 Codex 中调用：

> 使用 $psp-iso-to-pc-remaster，处理我提供的 PSP ISO，制作 Windows 原生高清试玩包，并说明实际验证范围。

续做或精简包示例：

> 使用 $psp-iso-to-pc-remaster，把当前修复版做成更小的原画质鼠标包，保留已有功能；实际游玩由我测试。

安卓高清版示例：

> 使用 $psp-iso-to-pc-remaster，把已有 PC 高清工程适配为安卓横屏 APK，目标手机是红米 Turbo 3。生成首次安装完整包和保留存档的后续更新小包；手机尚未连接，请准确列出已做检查和待真机验证内容。

保留现有技能名；新增 Android 入口，无需另装第二个 skill。Android 流程需要实际移植宿主后端，不是把 Windows EXE 重新封装。

## 文件与工具

| 文件 | 用途 |
|---|---|
| [SKILL.md](SKILL.md) | 触发范围、阶段入口、关键约束和完成条件 |
| [输入与目录](references/intake-and-layout.md) | ISO 获取、辨认、工具来源与工作目录 |
| [原生适配](references/native-porting.md) | 重编译、兼容修复、鼠标和阅读控制 |
| [高清与无损存储](references/hd-assets.md) | 素材清单、字库、表情图集和像素验证 |
| [便携发布](references/portable-release.md) | 资源去重、启动器、依赖和归档验证 |
| [安卓适配与分发](references/android-release.md) | 原生后端、高清渲染、触控、资源安装、签名升级和手机验收 |
| [本次案例](references/railgun-case-study.md) | ULJS00354 / PC HD8.6 / Android A0.2 的过程、体积与限制 |
| [convert_rgba_pack.py](scripts/convert_rgba_pack.py) | 将已有 RGTX RGBA 包转换为更小的无损存储 |
| [portable_archive.py](scripts/portable_archive.py) | 从干净 staging 生成并验证 ZIP 与外置哈希清单 |
| [android_hd_pack.py](scripts/android_hd_pack.py) | 生成与验证供安卓安装器使用的高清 ZIP_STORED 和逐文件清单 |

## 运行辅助脚本

需要 Python 3.11+。贴图转换另需 Pillow，WebP 模式要求 Pillow 带相应编解码支持；打包脚本仅用标准库。

```powershell
python -m pip install Pillow
python scripts/convert_rgba_pack.py --help
python scripts/portable_archive.py --help
python scripts/android_hd_pack.py --help
```

贴图转换示例（输入必须是已存在的原始 RGTX 包）：

```powershell
python scripts/convert_rgba_pack.py --source work/hd-raw --output work/hd-stored-v1 --report work/hd-stored-v1-audit.json --codec best
```

打包示例：

```powershell
python scripts/portable_archive.py build --stage work/stages/Game-HD-v1 --archive outputs/Game-HD-v1.zip --manifest outputs/Game-HD-v1-manifest.json
python scripts/portable_archive.py verify --archive outputs/Game-HD-v1.zip --manifest outputs/Game-HD-v1-manifest.json --report outputs/Game-HD-v1-verification.json
```

已有安卓高清素材打包示例（不会生成图像或构建 APK）：

```powershell
python scripts/android_hd_pack.py build --source work/mobile-hd-stage --archive outputs/Game-HD-Android-v1.zip --manifest outputs/Game-HD-Android-v1.json --version game-hd-v1
python scripts/android_hd_pack.py verify --archive outputs/Game-HD-Android-v1.zip --manifest outputs/Game-HD-Android-v1.json --source work/mobile-hd-stage
```

安卓 ZIP 使用根目录相对路径，清单字段与 Windows 便携包不同；接入目标应用的安装器时需匹配此格式。脚本保留素材字节，目标解码器是否支持它们必须另验。

输出目录/文件必须是新路径；工具拒绝覆盖已有交付物。具体参数、支持格式和边界见对应参考文档。

## 验证与边界

创建时完成了 skill 结构、UTF-8、内部引用和脚本语法检查。贴图脚本通过了混合透明度及隐藏 RGB 的 PNG / WebP / best 无损转换检查；打包脚本通过 13 项合成检查。独立试用还串联了转换、打包、校验，并验证了归档篡改会被拒绝。

安卓案例还完成了真实素材全量解码、生产 Renderer 离屏读回、资源安装器回归及约 994 MB 真实包的流式安装验证。**Android A0.2 尚未真机验证**；视频和 AUTO/SKIP 控件未启用。新加入的通用打包脚本只验证 ZIP/清单与原 staging，不代替案例中的手机宿主或安装器。

`android_hd_pack.py` 通过 25 项回归检查，覆盖字节保真、篡改、路径/类型/配额、覆盖保护和失败清理；生成的合成包被实际安装器接受，并独立核验了 1646 文件的现有高清包。测试修正了 Windows Python 3.13 文件时间元数据和 `fsync` 的兼容问题。

这些检查不证明任意游戏已经可玩。Pillow 回读一致、生产解码器一致、GPU 渲染正确、特定剧情可玩和完整通关，是不同层级的证据；电脑 ANGLE/JVM 的结果不能作为安卓真机证据。

本仓库仅包含 skill 文档和通用 Python 辅助脚本，不包含游戏 ISO、APK、专有生成代码、图片/音频、存档、签名私钥或编译工具链，也不自带游戏特定移植补丁。原游戏素材、高清素材与第三方运行时的许可范围分别处理；本地打包不会自动上传或向他人发送。
