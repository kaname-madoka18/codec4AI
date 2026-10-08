# 本地发行产物

[English](README.md) | 简体中文

运行 `bash tools/build_release.sh`，即可创建以版本号命名的子目录，其中包含 wheel、
源码分发包、对应源码归档、构建清单和 SHA256 校验和。Git 会忽略二进制产物；
这些文件应作为发行版附件发布，而不是提交到源码仓库。

详见[发布教程](../docs/RELEASING_cn.md)。本地文件不会自动上传到 GitHub 或 PyPI。
