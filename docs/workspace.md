# 工作区与仓库同步

本文定义小汐项目的标准本地布局、Git 工作流和生产同步边界。目标是让本机、GitHub 和生产服务器始终能追溯到同一个提交，同时避免临时审计副本和部署包长期堆积。

## 标准布局

工作区父目录只保留一个正式 checkout 和一个参考资料目录：

```text
qqbot/
├── xiaoxi-bot/             小汐唯一正式仓库
└── references/             外部参考项目，各自保留独立 Git 状态
```

- `xiaoxi-bot/` 是唯一允许开发、提交、推送和部署的小汐仓库。
- `references/` 不属于小汐仓库，不得复制进 `xiaoxi-bot/`，也不参与小汐的提交、测试或部署。
- 参考项目可能有自己的未提交修改或本地提交。移动工作区时必须完整保留，除非任务明确要求，不清理其内部文件。
- 不在工作区父目录长期保留 `audit-live-*`、`server-*`、`work*`、`source`、`deploy-staging` 等重复 checkout 或审计快照。

Windows 的推荐位置是 `D:\project\qqbot\xiaoxi-bot`。文档中的路径只是示例；真正的判定标准是该目录为唯一正式 checkout。

## 临时文件

- 临时审计、解包和部署验证目录放到系统临时目录，例如 Windows 的 `$env:TEMP\xiaoxi-bot-*` 或 Linux 的 `/tmp/qqbot-*`。
- Git bundle、tar 包、补丁、日志、二维码、虚拟环境和下载缓存不得提交，也不得长期放在工作区父目录。
- 临时目录必须在任务完成并验证无回滚需求后删除。
- 需要回滚时只保留一个已验证的代码归档；生产自动备份另按运维保留策略管理。

## Git 流程

1. 在唯一正式 checkout 中执行 `git fetch --prune --tags`，确认 `main` 与 `origin/main` 的关系。
2. 从最新 `main` 创建任务分支，完成修改和本地验证。
3. 推送任务分支，通过 PR 合并到 `main`，禁止 force-push。
4. 生产服务器执行 `git fetch --prune origin`，然后用 fast-forward 更新到 `origin/main`；不要在服务器直接编辑或提交。
5. 部署完成后核对本机、GitHub `main` 和 `/opt/qqbot` 的完整 SHA。
6. 确认本机和服务器工作树干净，再删除任务分支、临时目录和传输文件。

## 三方核对

本机：

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
```

GitHub：

```bash
git ls-remote origin refs/heads/main
```

生产服务器：

```bash
git -C /opt/qqbot status --short --branch
git -C /opt/qqbot rev-parse HEAD
systemctl is-active qqbot.service napcat.service
systemctl show qqbot.service napcat.service -p NRestarts
```

三个提交 SHA 必须完全一致。生产配置 `/var/lib/qqbot/config.json`、运行数据 `/opt/qqbot/data/` 和密钥 `/etc/qqbot.env` 不属于代码同步范围，任何工作区清理都不得删除或覆盖它们。
