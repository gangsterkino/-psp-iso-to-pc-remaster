# PSP ISO 到 PC 高清重制包 Skill

可供 Codex 调用的工作流程：将用户提供的 PSP ISO，逐步做成 Windows 原生 PC 试玩包，并完成鼠标适配、高清资源、无损压缩和便携包验证。

技能名：`psp-iso-to-pc-remaster`。

## 覆盖范围

1. 镜像获取、身份与格式检查，记录来源、版本和 SHA-256。
2. 原生重编译、运行时兼容修复和分阶段游玩验证。
3. 鼠标操作，以及按游戏情况接入 AUTO / SKIP。
4. 背景、角色图集、字库、UI 与动态表情的高清处理。
5. ISO 资源去重、PNG / WebP 无损存储和加载性能检查。
6. 原画质与高清分支、相对路径启动器、依赖收集、便携 ZIP 和完整性验证。

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

## 文件与工具

| 文件 | 用途 |
|---|---|
| [SKILL.md](SKILL.md) | 触发范围、阶段入口、关键约束和完成条件 |
| [输入与目录](references/intake-and-layout.md) | ISO 获取、辨认、工具来源与工作目录 |
| [原生适配](references/native-porting.md) | 重编译、兼容修复、鼠标和阅读控制 |
| [高清与无损存储](references/hd-assets.md) | 素材清单、字库、表情图集和像素验证 |
| [便携发布](references/portable-release.md) | 资源去重、启动器、依赖和归档验证 |
| [本次案例](references/railgun-case-study.md) | ULJS00354 / HD8.6 的实际过程、体积变化和限制 |
| [convert_rgba_pack.py](scripts/convert_rgba_pack.py) | 将已有 RGTX RGBA 包转换为更小的无损存储 |
| [portable_archive.py](scripts/portable_archive.py) | 从干净 staging 生成并验证 ZIP 与外置哈希清单 |

## 运行辅助脚本

需要 Python 3.11+。贴图转换另需 Pillow，WebP 模式要求 Pillow 带相应编解码支持；打包脚本仅用标准库。

```powershell
python -m pip install Pillow
python scripts/convert_rgba_pack.py --help
python scripts/portable_archive.py --help
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

输出目录/文件必须是新路径；工具拒绝覆盖已有交付物。具体参数、支持格式和边界见对应参考文档。

## 验证与边界

创建时完成了 skill 结构、UTF-8、内部引用和脚本语法检查。贴图脚本通过了混合透明度及隐藏 RGB 的 PNG / WebP / best 无损转换检查；打包脚本通过 13 项合成检查。独立试用还串联了转换、打包、校验，并验证了归档篡改会被拒绝。

这些合成检查不证明任意游戏已经可玩。Pillow 回读一致、生产解码器一致、GPU 渲染正确、特定剧情可玩和完整通关，是不同层级的证据。案例保留了视频、完整剧情及跨电脑测试等未完成项目。

本仓库仅包含 skill 文档和通用 Python 辅助脚本，不包含游戏 ISO、专有生成代码、图片/音频、存档或编译工具链。原游戏素材、高清素材与第三方运行时的许可范围分别处理；本地打包不会自动上传或向他人发送。
