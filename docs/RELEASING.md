# 发布安装包与源码

从 `v0.7.0` 起，发布目标为可在其他 Mac 上运行的应用安装包：macOS 15 或更新版本，分别提供 Apple Silicon `arm64` 与 Intel `x86_64`。应用内置 Python、Python 依赖、SQLCipher 和 FFmpeg / ffprobe，用户无需安装 Python 或 Homebrew。源码 ZIP 继续单独提供，不能与应用安装包混淆。

## 用户安装

从项目 [Releases](https://github.com/SeanLi-Coder/wechat-history/releases) 下载与芯片架构对应的 DMG 或 `.app.zip`。打开 DMG 或解压 ZIP 后，把“拾聊.app”放到“应用程序”，从该固定位置启动。安装包的界面显示在默认浏览器中。

当前应用使用 ad-hoc 签名，没有 Developer ID 签名或 Apple 公证。首次打开可能被 Gatekeeper 拦截。用户确认来源并核对 SHA-256 后，可按 [Apple 官方说明](https://support.apple.com/en-us/102445)，先尝试打开，再到“系统设置 → 隐私与安全性 → 仍要打开”。受管理电脑可能不允许此操作。不得声称无系统提示，不能用关闭 Gatekeeper、清除 quarantine 或修改 TCC 的命令代替安装说明。

已有可读 ZIP 可以直接打开或导入，不需要数据库密钥。新电脑上的原生微信读取仍需自己的有效密钥；手动初始化依赖微信版本、系统 LLDB 和匹配的 Apple Python，目前仅实现原生 Apple Silicon。安装包不自动取钥，也不保证所有微信版本或所有 Mac 的初始化均适用。

## 构建固定版本

在已审阅、已提交的源码上构建。每个架构使用同架构的 Mac，要求 Xcode Command Line Tools 与构建用 Python 3.11；这些是维护者的构建依赖，不是安装包用户的运行依赖。构建需要联网下载官方源码与 Python 运行时；原生工具源码和 Python 运行时按脚本中固定的摘要核验。Python 包版本由 `requirements-portable.txt` 及其引用文件固定，实际取得的 wheels 摘要另记录在构建材料中。

Apple Silicon 示例：

```bash
python3 scripts/build_tools.py \
  --output dist/portable-tools \
  --arch arm64 \
  --min-macos 15.0 \
  --cache dist/portable-cache

python3 scripts/build_portable.py \
  --arch arm64 \
  --tools dist/portable-tools \
  --output-dir dist/installers \
  --cache dist/portable-cache
```

Intel 构建将两条命令中的 `arm64` 改为 `x86_64`，并在 Intel 构建机上运行。不同架构应使用独立的输出与缓存目录，不要混用本机 Homebrew 二进制作为发布工具。

工具脚本从固定官方源码原生编译：FFmpeg 禁用 GPL、nonfree 和网络能力；SQLCipher 使用系统 CommonCrypto。应用使用固定的 python-build-standalone `20261009` / CPython `3.11.17`，并保留运行时及依赖的许可文件。确切版本、来源摘要和编译参数以构建脚本与输出清单为准。Python standalone 本身的系统下限不代表整个应用的兼容范围；本项目安装包统一要求 macOS 15+，构建检查覆盖内置二进制的架构、系统下限与动态库依赖。

`build_portable.py` 负责组装应用、检查依赖、签名及产物校验，并在移动应用位置、隔离用户目录和限制宿主依赖的条件下运行启动、媒体与归档合成检查。检查失败时不得发布该次输出。此流程不需要真实微信账号，不读取生产聊天库，也不运行取钥辅助程序。

以 `arm64` 为例，`dist/installers/` 输出以下四个文件，每个另附一份同名 `.sha256`，共八个文件：

- `shiliao-0.7.0-macos-arm64.dmg`；
- `shiliao-0.7.0-macos-arm64.app.zip`；
- `shiliao-0.7.0-macos-arm64-manifest.json`；
- `shiliao-0.7.0-macos-arm64-third-party-sources.zip`。

Intel 文件名中的架构为 `x86_64`。清单记录系统下限、架构、签名状态、检查结果及资产摘要；第三方源码 ZIP 提供对应版本的源码、许可和构建材料。应用包内的 `Contents/Resources/` 保留 `THIRD_PARTY_NOTICES.md`、`DEPENDENCIES.json`、`python/` 中的上游与各 Python 包许可，以及 `tools/licenses/`。

发布时保留这些材料的对应关系，不要只上传应用而遗漏校验文件或第三方源码材料。许可详情见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

在下载产物所在目录核对摘要：

```bash
shasum -a 256 -c *.sha256
```

安装包包含签名和构建工具生成的内容，不承诺跨机器逐字节可重现；应记录源码 commit、运行时和工具版本，并保留构建清单。

## GitHub Actions 双架构构建

[portable.yml](../.github/workflows/portable.yml) 仅由 `workflow_dispatch` 手动触发。`ref` 必须是已推送到仓库的完整、小写 40 位 commit SHA；工作流先校验格式，checkout 后再次验证 HEAD，确保两个架构构建相同的源码，避免移动分支或标签产生差异。

```bash
gh workflow run portable.yml --ref main -f ref="$(git rev-parse HEAD)"
```

构建矩阵固定为 `macos-15` / `arm64` 和 `macos-15-intel` / `x86_64`，对应 [GitHub 官方 runner 列表](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。不使用可能更换系统版本或架构的 `macos-latest`。每个 job 配置 Python 3.11，执行上述两条构建命令，成功后将 `dist/installers/` 上传为 `shiliao-macos-arm64` 或 `shiliao-macos-x86_64` artifact，保留 14 天。

工作流只有 `contents: read` 权限，不创建 Release、不上传 Release 资产、不安装签名证书或操作钥匙串。维护者下载并验收两个架构的产物，确认来源 commit 与目标版本一致后，再单独发布到对应标签。不要以“一个架构成功”替代另一个架构的实际构建和检查。

若未来提供 Developer ID 签名和公证，应单独配置受控签名流程，并按 [Apple 公证要求](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) 验证所有可执行文件、Hardened Runtime、时间戳与公证票据。当前 ad-hoc 验证通过不等于获得 Developer ID 或公证，也不赋予任何系统隐私权限。

## 源码运行与本机开发入口

源码包不包含预装运行时。使用 macOS、Python 3.10+，完整读取与媒体功能需要另行安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
brew install sqlcipher ffmpeg
python app.py
```

安装 SILK 依赖可能需要 Xcode Command Line Tools。原始 ZIP 备份和通用文字导入可仅使用 Python 标准库。

原有命令继续用于开发者本机：

```bash
python scripts/build_app.py
open dist/拾聊.app
```

`build_app.py` 把当前电脑的 Python 绝对路径写入应用包。这个输出只是本机入口，不能当作可分发安装包；移动应用或更换 Python 后需重建，并重新保存定时设置。安装包必须使用前述 `build_portable.py` 流程生成。

## 源码发布资产与范围

```bash
python3 -B scripts/package_release.py --check --version v0.7.0
python3 -B scripts/package_release.py --version v0.7.0
```

默认在 `dist/releases/` 输出 `shiliao-0.7.0-source.zip` 与对应 `.sha256`。ZIP 顶层为 `shiliao-0.7.0/`，`RELEASE-MANIFEST.json` 记录逐文件相对路径、字节数与 SHA-256。版本读取 `vault/__init__.py` 的字面量；传入不一致的版本会失败，不执行该模块。

唯一源码打包名单是 `scripts/package_release.py` 的 `RELEASE_FILES`，不使用递归通配。添加脚本、测试、工作流或文档时，必须审阅并显式更新名单。可用 `--list` 核对；可用 `--output-dir` 指定空输出目录。已有同名文件不会被覆盖。

源码清单仅包含应用源码、静态界面、图标、合成测试、必要脚本、依赖清单、CI、文档与审阅过的 vendor 材料。不会打包 `.git/`、`.test-artifacts/`、`dist/`、虚拟环境、截图、应用状态、真实数据库、聊天导出、媒体、原始备份、账号材料、密钥或逆向日志。文件或任一级父目录是符号链接时失败；可识别的凭证、个人绝对 home 路径等检查也必须通过。检查不会自动辨认所有真实聊天正文，不能替代人工审阅。

源码 ZIP 固定文件顺序、时间戳、权限和压缩级别，相同源码及 Python / zlib 工具链可得到相同字节；不承诺不同压缩库版本下字节一致。GitHub 自动生成的源码 ZIP / tar.gz 来自提交和标签，范围不受此打包名单控制，因此提交内容仍需逐项审阅。

## 发布验收

运行源码合成测试，并检查源码发布范围：

```bash
python3 -m pip install -r requirements-dev.txt
python3 -B -m unittest discover -s tests -v
python3 -B scripts/browser_smoke.py
python3 -B scripts/search_smoke.py
python3 -B scripts/package_release.py --check --version v0.7.0
```

浏览器测试需要声明的 Playwright / Chrome 环境；不得上传真实聊天截图或私密验证输出。`scripts/test_key_capture.py` 是人工进程和人工数据库的独立 LLDB 检查，不属于普通安装包构建步骤。

确认两架构安装包的构建与内置检查均成功，核对 SHA-256、构建清单与源码材料，实际从最终交付文件解压或挂载并检查应用，再发布安装包和源码。更新已安装的应用时不要把运行中的旧进程当作新构建验证，也不要让测试访问生产状态目录。

发布说明应保留：macOS 15+ 与架构要求、当前签名和公证状态、原生微信初始化的版本限制、缺失本地媒体无法恢复、WXGF / MPO 展示限制，以及原始和可读 ZIP 均不额外加密。运行时内置不意味着系统权限已授予，也不意味着支持所有微信版本。
