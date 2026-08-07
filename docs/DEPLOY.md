# 部署到阿里云 ECS

以 Ubuntu / Alibaba Cloud Linux 为例。目标：服务常驻、开机自启、可远程访问。

## 1. 上服务器装环境
```bash
ssh root@<你的公网IP>
# Python 3.10+ 与 git（阿里云镜像更快）
sudo apt update && sudo apt install -y python3 python3-pip git    # Ubuntu
# CentOS/Alibaba Cloud Linux: sudo yum install -y python3 python3-pip git
```

## 2. 拉代码
```bash
sudo mkdir -p /opt/TradeAssistant && sudo chown $USER /opt/TradeAssistant
git clone git@github.com:tianruochen/TradeAssistant.git /opt/TradeAssistant
# 若 ECS 连不上 github，可在本机打包上传：
#   本机: tar --exclude=.git --exclude=data/users --exclude=logs -czf ta.tgz -C ~/projects TradeAssistant
#         scp ta.tgz root@IP:/opt/ && ssh 上 tar xzf
cd /opt/TradeAssistant
pip3 install -r requirements.txt      # akshare/pandas 较大,耐心等
```

## 3. 配置密钥
```bash
cp secrets.env.example secrets.env
vi secrets.env        # 填 LLM_API_KEY；TA_OWNER_UID 先留空,注册后再填
```

## 4. 常驻运行（二选一）
**A. systemd（推荐）**
```bash
sudo cp deploy/tradeassistant.service /etc/systemd/system/
sudo sed -i "s/CHANGE_ME_USER/$USER/" /etc/systemd/system/tradeassistant.service
# 若 python3 不在 /usr/bin，改 ExecStart 里的路径(which python3)
sudo systemctl daemon-reload && sudo systemctl enable --now tradeassistant
sudo systemctl status tradeassistant
journalctl -u tradeassistant -f
```
**B. 自带脚本**：`./tradeagent start`（nohup + 日志到 logs/）。

## 5. 放行端口 + 访问
- 阿里云控制台 → ECS → 安全组 → 入方向加规则：TCP **8760**，**来源限你自己的 IP**（别对 0.0.0.0 全开）。
- 浏览器开 `http://<公网IP>:8760`，注册首个账户即业主号。
- 拿到 uid：`sqlite3 data/users.db "select uid,username from users;"` → 填回 `secrets.env` 的 `TA_OWNER_UID` → 重启服务（定时任务/告警才会落到你账户）。

## 6.（建议）Nginx + HTTPS + 域名
公网裸跑 8760 不安全。建议 nginx 反代 + 证书，并只放行 443：
```nginx
server {
  listen 443 ssl;
  server_name your.domain.com;
  ssl_certificate     /etc/letsencrypt/live/your.domain.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/your.domain.com/privkey.pem;
  location / {
    proxy_pass http://127.0.0.1:8760;
    proxy_http_version 1.1;
    proxy_set_header Connection '';      # SSE 流式必须
    proxy_buffering off;                 # SSE 流式必须
    proxy_read_timeout 300s;
    proxy_set_header Host $host;
  }
}
```
证书用 `certbot --nginx`。此时安全组只放行 443，8760 只监听本机。

## 安全提醒
- `/api/register` 默认开放（任何人可注册）。个人用建议：安全组锁自己 IP，或注册完你自己的账户后，在 `server.py` 把 `register_handler` 关掉/加邀请码。
- LLM 中转会经手对话内容；生产建议换官方 API。
- `secrets.env` 与 `data/` 不入库，服务器上单独维护、注意备份 `data/`。
