#!/usr/bin/env bash
# mx-trader-bridge systemd 安装脚本（需 sudo）
set -e

SERVICE_DIR=/etc/systemd/system
SERVICE_FILE=mx-trader.service
SOURCE=/home/ubuntu/projects/mx-trader-bridge/deploy/$SERVICE_FILE

if [[ $EUID -ne 0 ]]; then
   echo "❌ 需要 sudo 权限：sudo bash $0"
   exit 1
fi

echo "→ 复制 service 文件"
cp "$SOURCE" "$SERVICE_DIR/$SERVICE_FILE"

echo "→ reload systemd"
systemctl daemon-reload

echo "→ 启用开机自启（不立即启动）"
systemctl enable mx-trader.service

cat <<EOF

✅ 已安装：$SERVICE_DIR/$SERVICE_FILE

启停命令：
  sudo systemctl start    mx-trader     # 启动
  sudo systemctl stop     mx-trader     # 停止
  sudo systemctl restart  mx-trader     # 重启
  sudo systemctl status   mx-trader     # 状态
  journalctl -u mx-trader -f            # systemd 日志（极少，主要看 logs/）
  tail -f /home/ubuntu/projects/mx-trader-bridge/logs/scheduler-\$(date +%F).log
EOF
