# 第三方来源与许可证

本文件说明源码包中的第三方代码，以及从 v0.7.0 起随 macOS 应用安装包分发的运行时与工具。项目自有代码当前未单独指定开源许可证；各第三方组件仍按其对应许可分发，本说明不替代或缩减那些许可规定的权利与义务。

## 随源码分发的代码

`vendor/wechat-access/capture_keys.py` 来自 [acmerfight 的 macOS WeChat 4.x local toolkit](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f)，许可证为 MIT。完整版权与许可文本保存在 [vendor/wechat-access/LICENSE](vendor/wechat-access/LICENSE)，随源码包一起提供。

本次核验使用 GitHub 官方 Gist API，版本为 `c98dbb10cf23c30e83d5d744905101b0880887a0`。本地 `capture_keys.py` 和 `LICENSE` 与该版本的上游文件字节一致。

| 文件 | SHA-256 |
| --- | --- |
| `capture_keys.py` | `0b0b9f9296bf02219c07537827ed9ce97671611ffeab36572bedb267a7704d65` |
| `LICENSE` | `72380798af61d6131287b0dd3c8dc5345535df641002fece3a02d7ef109c5f8e` |

上游版权声明为 `Copyright (c) 2026 Contributors`。可从 [固定版本](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f/c98dbb10cf23c30e83d5d744905101b0880887a0) 核对文件及许可证。发布包没有包含上游的聊天发送、OCR、屏幕采集或自动回复工具。

`vault/media_codecs.py` 中的 DAT 布局与 macOS 图片密钥派生逻辑参考同一 MIT 许可 Gist 的 `media_pipeline.py`，在本项目中加入文件一致性检查、格式验证、私密临时文件、原件保存和本地归档接口。对应 MIT 许可文本同样保留在上述路径。

## 随应用安装包分发的依赖

macOS 15+ 的 `arm64` 和 `x86_64` 安装包内置 Python、所需 Python 包、SQLCipher 及 FFmpeg / ffprobe。它们来自构建脚本指定的版本及摘要，不使用维护者个人 Python 环境或 Homebrew 安装目录作为发布内容。

- Python 使用 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) `20261009` 的 CPython `3.11.17`；随运行时保留其许可材料。这个发行版包含的依赖也有各自许可，不能只按 Python 主许可证概括。
- SQLCipher 从官方固定源码构建，使用 Apple 系统 CommonCrypto。社区版许可与版权声明随构建材料保留。
- FFmpeg / ffprobe 从官方固定源码构建，禁用 GPL、nonfree 组件及网络能力。确切组件、编译选项与许可版本以该安装包的构建清单、构建脚本及 FFmpeg 许可文件为准，不以其他 FFmpeg 发行版的设置代替。
- `pilk` 使用 GPL-3.0，其确切版本的对应源码、许可和构建材料随第三方源码包提供。FFmpeg 的构建选项不会改变 `pilk` 自己的许可证。
- Python 包保留各自发行材料中的许可证与版权声明；涉及的内置库、Rust / C 组件等以对应材料为准。

每个架构的安装产物附带构建清单、SHA-256 校验文件和 `shiliao-0.7.0-macos-<arch>-third-party-sources.zip` 源码材料包。维护者发布时必须一并保留相关源码、许可和构建说明，确保它们与实际二进制版本对应。应用的 `Contents/Resources/THIRD_PARTY_NOTICES.md` 和 `DEPENDENCIES.json` 说明依赖；Python 上游及各包的许可保存在 `Contents/Resources/python/` 对应位置，工具许可位于 `Contents/Resources/tools/licenses/`。安装包中的许可文件和源码材料是本说明的组成部分；具体内容以实际构建输出为准。不要将“源码已公开”或提供上游链接视作可以省略所分发组件对应材料的理由。

应用安装包不分发 Apple LLDB、微信本身或私有系统框架。手动取钥所需的 Apple 调试工具仍由用户另行准备；普通归档阅读不需要这些工具。

## 组件来源

| 组件 | 用途 | 上游许可或项目说明 |
| --- | --- | --- |
| Python | 运行应用和合成测试 | [Python 许可](https://docs.python.org/3/license.html) |
| python-build-standalone | 可重新分发的 Python 运行时 | [项目与许可说明](https://github.com/astral-sh/python-build-standalone)，并保留发行包内各依赖许可 |
| cryptography | 图片解密及密码学合成测试 | [许可证说明](https://cryptography.io/en/latest/licenses/) |
| Pillow | 图片识别与完整性验证 | [许可文件](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| python-zstandard | 微信消息正文解压 | [许可文件](https://github.com/indygreg/python-zstandard/blob/main/LICENSE) |
| pilk | 本机 SILK 解码 | [GPL-3.0 许可文件](https://github.com/foyoux/pilk/blob/main/LICENSE) |
| SQLCipher | 在私密副本中恢复加密数据库及 WAL | [社区版许可文件](https://github.com/sqlcipher/sqlcipher/blob/master/LICENSE) |
| FFmpeg / ffprobe | 音视频转换、无损重封装与校验 | [许可说明](https://www.ffmpeg.org/legal.html)，实际条件取决于构建选项 |
| Playwright | 开发阶段浏览器测试，不作为应用运行时分发 | [许可文件](https://github.com/microsoft/playwright/blob/main/LICENSE) |

源码 ZIP 本身仍不包含预装的 Python、SQLCipher、FFmpeg、Python wheels 或编译产物。从源码运行时，用户另行安装的版本及其额外依赖可能与应用安装包不同，应同时保留并查阅所安装发行包的许可材料。

微信是其权利人的产品。本项目不分发微信应用、私有二进制、用户数据库、账号凭据或聊天内容；运行时产生的个人数据不属于发布资产。
