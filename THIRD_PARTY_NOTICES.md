# 第三方来源与许可证

本文件说明源码包中保留的第三方代码与外部运行依赖。项目自有代码当前未指定开源许可证；此说明不新增或改变其许可，也不把某个依赖的许可证套用到整个项目。

## 随源码分发的代码

`vendor/wechat-access/capture_keys.py` 来自 [acmerfight 的 macOS WeChat 4.x local toolkit](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f)，许可证为 MIT。完整版权与许可文本保存在 [vendor/wechat-access/LICENSE](vendor/wechat-access/LICENSE)，随源码包一起提供。

本次核验使用 GitHub 官方 Gist API，版本为 `c98dbb10cf23c30e83d5d744905101b0880887a0`。本地 `capture_keys.py` 和 `LICENSE` 与该版本的上游文件字节一致。

| 文件 | SHA-256 |
| --- | --- |
| `capture_keys.py` | `0b0b9f9296bf02219c07537827ed9ce97671611ffeab36572bedb267a7704d65` |
| `LICENSE` | `72380798af61d6131287b0dd3c8dc5345535df641002fece3a02d7ef109c5f8e` |

上游版权声明为 `Copyright (c) 2026 Contributors`。可从 [固定版本](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f/c98dbb10cf23c30e83d5d744905101b0880887a0) 核对文件及许可证。发布包没有包含上游的聊天发送、OCR、屏幕采集或自动回复工具。

`vault/media_codecs.py` 中的 DAT 布局与 macOS 图片密钥派生逻辑参考同一 MIT 许可 Gist 的 `media_pipeline.py`，在本项目中加入文件一致性检查、格式验证、私密临时文件、原件保存和本地归档接口。对应 MIT 许可文本同样保留在上述路径。

## 由用户另行安装的依赖

本源码 ZIP 不包含 Python、LLDB、SQLCipher、FFmpeg、Python wheels 或它们的编译产物。安装版本及其依赖可能包含额外许可文本，具体以相应发行包为准。

| 组件 | 用途 | 上游许可或项目说明 |
| --- | --- | --- |
| Python | 运行应用和合成测试 | [Python 许可](https://docs.python.org/3/license.html) |
| cryptography | 图片解密及密码学合成测试 | [许可证说明](https://cryptography.io/en/latest/licenses/) |
| Pillow | 图片识别与完整性验证 | [许可文件](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| python-zstandard | 微信消息正文解压 | [许可文件](https://github.com/indygreg/python-zstandard/blob/main/LICENSE) |
| pilk | 本机 SILK 解码 | [GPL-3.0 许可文件](https://github.com/foyoux/pilk/blob/main/LICENSE) |
| SQLCipher | 在私密副本中恢复加密数据库及 WAL | [社区版许可文件](https://github.com/sqlcipher/sqlcipher/blob/master/LICENSE) |
| FFmpeg / ffprobe | 音视频转换、无损重封装与校验 | [许可说明](https://www.ffmpeg.org/legal.html)，实际条件取决于构建选项 |
| Playwright | 开发阶段浏览器测试 | [许可文件](https://github.com/microsoft/playwright/blob/main/LICENSE) |

如果将来制作包含上述依赖的安装器，应针对所分发的确切版本和构建选项另行整理许可及所需源码材料；当前源码发布不能替代这项核对。

微信是其权利人的产品。本项目不分发微信应用、私有二进制、用户数据库、账号凭据或聊天内容；运行时产生的个人数据不属于发布资产。
