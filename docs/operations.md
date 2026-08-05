# 运行与部署

## 本地验证

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m compileall -q -f .
QQBOT_DISABLE_FILE_LOG=1 python -m unittest discover -v
```

## 服务器布局

- 代码：`/opt/qqbot`，由 root 管理，运行用户只读。
- 配置：`/var/lib/qqbot/config.json`，权限 `0600`。
- 运行数据：`/opt/qqbot/data`，仅 `qqbot` 用户可写。
- 临时文件：`/opt/qqbot/data/tmp`，不写入代码目录。
- 日志：`/var/log/qqbot`，应用日志轮转并限制为 `0600`。
- journald：最多使用 `256 MB`，保留 `14` 天。
- PID：`/run/qqbot/bot.pid`。
- 密钥：`/etc/qqbot.env`，不进入 Git 或 JSON 配置。

## 首次安装或升级服务

```bash
sudo bash /opt/qqbot/deploy/install-qqbot-service.sh /opt/qqbot
```

安装脚本会创建系统用户、迁移并脱敏配置、安装 systemd 单元并重启服务。

## 检查

```bash
systemctl status qqbot.service --no-pager
journalctl -u qqbot.service -n 100 --no-pager
systemd-analyze security qqbot.service
```

## 回滚

部署前先记录提交：

```bash
git -C /opt/qqbot rev-parse HEAD
```

需要回滚时切换到已验证提交，重新运行安装脚本并检查日志。不要直接删除运行数据目录。
