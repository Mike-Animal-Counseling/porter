[English](THIRD_PARTY.md) | **中文**

# 第三方组件

Porter 自身源码采用 [MIT 许可证](LICENSE)。依赖保留各自的许可证；安装 Porter
不会改变这些项目的授权条件。

## 仓库内附带的浏览器代码

| 文件 | 上游版本 | 许可证 |
|---|---|---|
| [porter/static/htmx.min.js](porter/static/htmx.min.js) | [htmx 2.0.4](https://github.com/bigskysoftware/htmx/tree/v2.0.4) | [Zero-Clause BSD（0BSD）](https://github.com/bigskysoftware/htmx/blob/v2.0.4/LICENSE) |

当前文件的 SHA-256 为
`e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`。
更新时应核对来源、阅读上游发布记录与许可证，并验证面板导航、表单、局部更新及错误反馈。
加载此资源无需访问 CDN。

## 安装的依赖与模型权重

Python 依赖声明于 [pyproject.toml](pyproject.toml) 和
[API 依赖](api/requirements.txt)，另有独立的
[模型依赖](api/requirements-embedder.txt) 及已验证的版本约束文件。
各依赖的安装元数据包含其许可证信息。
[依赖审计](docs/dependency-audit.2026-10-20.json) 记录某一时间点的漏洞检查，
并不代表已完成许可证审计或未来不会出现漏洞。

BGE-M3 权重和可选的 Ollama 模型单独下载。重新分发或商用前，应核对所选模型的上游许可证
和使用条件；本仓库的 MIT 许可证不覆盖模型权重。

基础容器、PostgreSQL 与 pgvector 同样保留各自的许可证。
此清单说明附带或安装的组件，不是某一实际部署的完整软件物料清单。
