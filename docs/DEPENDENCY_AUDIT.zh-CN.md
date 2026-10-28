[English](DEPENDENCY_AUDIT.md) | **中文**

# 依赖漏洞公告核对

2026-10-20 已将两份公开约束文件中的 Python 包精确版本提交至官方
[OSV 批量查询 API](https://google.github.io/osv.dev/post-v1-querybatch/)。
最终 **94 个去重包名/版本组合没有匹配到漏洞公告记录**。这只是有日期的已知公告检查，
不代表项目没有漏洞。

[机器可读记录](dependency-audit.2026-10-20.json)保留了修复前发现及最终 `post_remediation`
复查结果。原始 96 个组合中，`filelock`、`setuptools` 在 core/model 各有两个不同版本；
统一约束后最终查询数降至 94。

| 原始发现 | 最终约束变化 |
| --- | --- |
| `certifi 2026.7.22`：受影响的 CA 证书，[GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) | `certifi 2026.10.19` |
| 模型 `setuptools 78.1.0`：旧下载器路径穿越，[CVE-2025-47273](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf) | 两份约束均为 `setuptools 84.0.0` |
| 模型 `setuptools 78.1.0`：macOS 源码打包排除绕过，[CVE-2026-59890](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c) | 两份约束均为 `setuptools 84.0.0` |
| `filelock`、`setuptools` 的 core/model 精确版本互相冲突 | 两份约束均为 `filelock 4.2.3`、`setuptools 84.0.0` |

原始六条 OSV 记录实际对应三个独立公告，PYSEC 条目是别名，不是额外三个漏洞。
部分上游公告页与 OSV 的修复版本字段不同；记录明确保留差异和最终查询结果，不假定两者一致。

## 覆盖范围与限制

- 只发送公开包名与精确版本，没有发送源码、环境变量、凭据或用户记忆。
- 约束中列出的开发、可选和平台专用依赖全部查询，没有根据环境标记排除包。
  最终文件哈希和完整查询列表保存在 `post_remediation`。
- 未扫描约束外依赖、容器系统包、模型权重或浏览器 vendor 库。
- 匹配公告不代表存在可达利用，零匹配也不证明安全。安装兼容性、应用测试、
  容器构建和模型验收需要独立验证。
- 两次 GitHub Dependabot 查询均为零条未解决告警，但默认分支索引可能滞后，
  也不一定覆盖本地检查分支，因此没有用它替代直接版本核对。

没有安装新的审计工具，也没有添加依赖联网的强制 CI 门槛。依赖更新后或发布前应重新记录有日期的
公告检查，保留结果并重新执行相关构建/运行验证。
见[维护者指南](MAINTAINERS.zh-CN.md)和[安全政策](../SECURITY.zh-CN.md)。
