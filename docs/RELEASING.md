# 源码发布

首次公开版本为 `v0.2.0`，下面以 `v0.5.0` 演示打包。Release 提供可审阅、可重新构建的源码 ZIP，运行前需要自行安装 Python 和相关依赖。它不是内置 Python、数据库工具和媒体解码器的通用安装包。

## 用户安装

需要 macOS、Python 3.10+。原始 ZIP 备份和通用文字导入使用 Python 标准库；完整的微信数据库与媒体读取还需要 SQLCipher 4.x、FFmpeg / ffprobe，以及 `requirements.txt` 中的 Python 包。

在解压后的源码目录运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
brew install sqlcipher ffmpeg
python app.py
```

安装 SILK 依赖可能需要 Xcode Command Line Tools。首次取钥辅助程序另外依赖系统 LLDB 和与其匹配的 Apple Python，目前仅实现原生 Apple Silicon；不得把它描述成所有微信版本或所有 Mac 都适用的自动初始化。已有可读导出文件或归档的用户不需要执行取钥步骤。

需要本机应用入口时，可在安装依赖后运行：

```bash
python scripts/build_app.py
open dist/拾聊.app
```

此命令把当前电脑的 Python 绝对路径写入新建的应用包，并使用本机 ad-hoc 签名。因此 `dist/拾聊.app` 仅是构建者本机的启动器，不能作为本次 GitHub Release 的安装资产上传。它未包含第三方运行时，也没有 Developer ID 签名或公证。更新签名后的 macOS 权限是否可复用由系统决定；软件不会自动调整 TCC、SIP 或用户授权。

## 发布资产

在项目根目录执行：

```bash
python3 -B scripts/package_release.py --check --version v0.5.0
python3 -B scripts/package_release.py --version v0.5.0
```

默认输出到 `dist/releases/`：

- `shiliao-0.5.0-source.zip`
- `shiliao-0.5.0-source.zip.sha256`

ZIP 顶层目录为 `shiliao-0.5.0/`，附有 `RELEASE-MANIFEST.json`，逐个记录源码相对路径、字节数与 SHA-256。清单不包含构建机器路径、时间、账号、密钥或运行状态。版本直接读取 `vault/__init__.py` 的字面量声明；传入版本不一致会失败，不执行该模块。

可指定空的 `--output-dir`。已有同名 ZIP 或校验文件会被保留，脚本不会覆盖。构建中发现源文件变化时停止发布并清理临时产物。

验证下载：

```bash
cd dist/releases
shasum -a 256 -c shiliao-0.5.0-source.zip.sha256
```

打包器固定文件顺序、ZIP 时间戳、权限和压缩级别，不使用本机文件的修改时间。相同源码、相同 Python / zlib 工具链重复构建得到相同字节；不同压缩库版本之间不承诺 ZIP 字节完全相同，内部逐文件哈希仍可核对。

## 逐文件发布范围

唯一打包名单是 `scripts/package_release.py` 的 `RELEASE_FILES`。没有递归通配，也不会自动包含新增文件。添加源模块、测试或工作流时，先审阅文件，再显式更新此名单。

```bash
python3 -B scripts/package_release.py --list
```

名单仅包含应用 Python 源码、三个静态界面文件、应用图标、合成测试、必要脚本、依赖清单、CI 工作流、用户文档及 vendored 源码和许可证。`tests/test_search.py` 和 `scripts/search_smoke.py` 属于发布测试材料。

不会打包 `.git/`、`.test-artifacts/`、`dist/`、虚拟环境、浏览器截图、真实账号材料、应用状态目录、数据库、聊天导出、媒体、原始备份、私密取钥输出或逆向调查日志。即使它们出现在项目旁边，也不会被发现或复制。文件本身或任一级子目录是符号链接时，打包失败；文本文件中出现可识别的凭证、个人绝对 home 路径或未审阅账号标识，也会失败，只报告文件和规则，不回显匹配值。

上述检查不能替代人工审阅：例如普通正文无法可靠地自动区分真实聊天与合成文本。图标是名单中唯一的二进制资产，更换时也需要确认来源和内容。

GitHub 自动生成的源码 ZIP / tar.gz 来自提交和标签，范围不由本打包脚本控制。首次提交同样应按显式名单暂存，并检查 `git diff --cached --name-only` 和实际差异；不要把整个工作目录加入 Git。仓库发布与 Release 上传由维护者另行执行，打包脚本不会调用 Git、GitHub、网络或运行取钥程序。

## 发布前验证

安装开发依赖，并运行合成测试：

```bash
python3 -m pip install -r requirements-dev.txt
python3 -B -m unittest discover -s tests -v
python3 -B scripts/browser_smoke.py
python3 -B scripts/search_smoke.py
python3 -B scripts/package_release.py --check --version v0.5.0
```

浏览器测试需要其声明的 Playwright / Chrome 环境。测试使用合成数据，不能把本机真实聊天库、截图、取钥输出或私人验证日志作为附件提交。`scripts/test_key_capture.py` 是人工 C 子进程与人工数据库的独立 LLDB 验证，可在满足 Apple Silicon / LLDB 条件的机器手动执行；普通打包不运行它，也不启动微信。

发布说明应保留以下事实：源码安装、依赖需自行安装、微信版本兼容范围有限、缺失的本地媒体无法恢复、WXGF / MPO 的展示限制，以及原始和可读 ZIP 均不额外加密。不得宣称通用免安装、自动获得系统权限或恢复所有未下载媒体。

第三方来源与许可证见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。项目自有代码当前未指定开源许可证；源码公开不表示将第三方 MIT 条款自动应用于整个项目。
