#!/usr/bin/env bash
# TradeAssistant 一键部署(阿里云 ECS / 通用 Linux)。在项目根目录运行:  bash deploy/setup.sh
# 幂等:可重复执行。装依赖 → 备 secrets.env → 装 systemd → 起服务。
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(pwd)"; USER_NAME="$(whoami)"; PY="$(command -v python3)"
echo "▶ 项目目录: $ROOT   用户: $USER_NAME   python: $PY"

echo "▶ 安装依赖..."; "$PY" -m pip install -q -r requirements.txt

if [ ! -f secrets.env ]; then
  cp secrets.env.example secrets.env
  echo "ℹ 已生成 secrets.env(LLM_API_KEY 通常留空——每个用户在网页里填自己的Key)"
fi

echo "▶ 安装 systemd 服务 tradeassistant ..."
UNIT=/etc/systemd/system/tradeassistant.service
sudo cp deploy/tradeassistant.service "$UNIT"
sudo sed -i "s#CHANGE_ME_USER#$USER_NAME#; s#/opt/TradeAssistant#$ROOT#; s#/usr/bin/python3#$PY#" "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now tradeassistant
sleep 2
sudo systemctl --no-pager --full status tradeassistant | head -6 || true

PORT="$(grep -E '^\s*port:' config.yaml | head -1 | grep -oE '[0-9]+' | head -1 || echo 8760)"
cat <<EOF

✅ 部署完成。
   本机自检:  curl -s localhost:$PORT/health
   看日志:    journalctl -u tradeassistant -f
   下一步:
   1) API Key 不用在这填——注册登录后在网页里填各自的 Key(业主也在网页填自己的Key,后台任务会用它)
   2) 阿里云安全组放行 TCP $PORT(来源限你的IP);或上 nginx+HTTPS(见 docs/DEPLOY.md, deploy/nginx.conf)
   3) 浏览器注册首个账户 → 取 uid 填回 secrets.env 的 TA_OWNER_UID:
      sqlite3 data/users.db "select uid,username from users;"
   4) 注册完后建议在 secrets.env 设 REGISTER_CODE=off 关闭注册,再 restart
EOF
