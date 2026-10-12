# Local key capture helper

`capture_keys.py` 来自 [acmerfight 的 macOS WeChat local toolkit](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f)，采用 MIT 许可证。上游许可证完整保留在同目录的 `LICENSE`。

已通过 GitHub 官方 Gist API 核对版本 `c98dbb10cf23c30e83d5d744905101b0880887a0`：此脚本与 `LICENSE` 均和上游字节一致。固定文件哈希见项目根目录 `THIRD_PARTY_NOTICES.md`。

仅在用户明确启动初始化时运行。它通过 LLDB 启动指定的临时微信副本，只接受与所选本地数据库头部匹配并通过 HMAC 验证的密钥；不会附加到已运行的原版微信进程，不发送消息或上传数据。密钥写入私密文件，不打印到终端。

环境准备、临时副本签名、阅读库与界面由项目自身模块提供。捕获仅实现原生 arm64，依赖系统 LLDB 及匹配的 Apple Python；合成测试不能保证任意 macOS、微信版本或企业设备策略下均能完成初始化。遇到系统权限拒绝时停止，由用户处理正常授权，不关闭 SIP 或更改权限数据库。
