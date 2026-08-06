# 运行与部署

## 本地验证

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
ruff check . --no-cache --select E9,F524,F63,F7,F82
bandit -q -r app bot deploy -x tests --severity-level high --confidence-level high
python -m compileall -q -f .
QQBOT_DISABLE_FILE_LOG=1 python -m unittest discover -v
```

## 服务器布局

- 代码：`/opt/qqbot`，由 root 管理，运行用户只读。
- 配置：`/var/lib/qqbot/config.json`，权限 `0600`。
- 运行数据：`/opt/qqbot/data`，仅 `qqbot` 用户可写。
- 临时文件：`/opt/qqbot/data/tmp`，不写入代码目录。
- 诊断文件：`/opt/qqbot/data/diagnostics`，服务重启后仍保留。
- 日志：`/var/log/qqbot`，应用日志轮转并限制为 `0600`。
- 聊天正文日志：生产服务默认通过 `QQBOT_DISABLE_CHAT_LOG=1` 关闭。
- journald：最多使用 `192 MB`，保留 `7` 天，单个日志文件最多保留 `1` 天。
- PID：`/run/qqbot/bot.pid`。
- 密钥：`/etc/qqbot.env`，不进入 Git 或 JSON 配置。
- 备份：`/root/qqbot-backups`，至少保留最新 `20` 份，只清理超过 `30` 天的普通文件。

## 首次安装或升级服务

```bash
sudo bash /opt/qqbot/deploy/install-qqbot-service.sh /opt/qqbot
sudo bash /opt/qqbot/deploy/install-napcat-service.sh /opt/qqbot
```

安装脚本会创建系统用户、迁移并脱敏配置、安装 systemd 单元、启用备份保留定时器并重启服务。NapCat 服务通过过滤器仅向 journald 输出生命周期、警告和错误信息，普通聊天事件会被抑制，敏感字段会被脱敏。

## 检查

```bash
systemctl status qqbot.service --no-pager
systemctl status napcat.service --no-pager
journalctl -u qqbot.service -n 100 --no-pager
journalctl -u napcat.service -n 100 --no-pager
systemd-analyze security qqbot.service
systemd-analyze security napcat.service
```

## 回滚

部署前先记录提交：

```bash
git -C /opt/qqbot rev-parse HEAD
```

需要回滚时切换到已验证提交，重新运行安装脚本并检查日志。不要直接删除运行数据目录。
