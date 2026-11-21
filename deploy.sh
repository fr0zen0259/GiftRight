#!/bin/bash
# GiftRight 云端启动脚本（Alibaba Cloud Linux 3 / 任意 Linux）
#
# 用法：
#   1. 把 giftright-deploy.tar.gz 传到服务器
#   2. bash deploy.sh
#
# 脚本会：解包 → 装 Python → 写配置 → 起服务 → 保活 → 打印访问地址

set -e

APP_DIR=/opt/giftright
TARBALL=$(dirname "$(readlink -f "$0")")/giftright-deploy.tar.gz

echo "=== 1. 解包 ==="
mkdir -p "$APP_DIR"
tar -xzf "$TARBALL" -C "$APP_DIR"
cd "$APP_DIR"
echo "解包完成，文件数：$(find . -type f | wc -l)"

echo
echo "=== 2. 检查 Python ==="
# app.py 已做 3.6 兼容（ThreadingHTTPServer 用 socketserver.ThreadingMixIn 兜底，
# str.isascii 换成 ord() 判断）。所以 3.6 就够，优先挑更高的版本。
py_ok() {
    [ -n "$1" ] || return 1
    v=$("$1" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null)
    [ -n "$v" ] && [ "$v" -ge 36 ] 2>/dev/null
}

PY=""
for cand in python3.11 python3.10 python3.9 python3.8 python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
        PY=$cand
        break
    fi
done

if [ -z "$PY" ]; then
    echo "错误：找不到 Python 3.6+，无法继续。"
    echo "请手动安装后重跑：dnf install -y python3"
    exit 1
fi
echo "Python: $($PY --version 2>&1)  (命令: $PY)"

echo
echo "=== 3. 写环境配置 ==="
# 如果已经存在 .env 就不覆盖，避免重跑时把密钥冲掉
if [ ! -f "$APP_DIR/.env" ]; then
    cat > "$APP_DIR/.env" <<'ENVEOF'
# GiftRight 环境配置
# 这两个密钥必须填（从本地 .env 或 Dify 后台拿）
DIFY_API_KEY=在这里填你的推荐工作流密钥
DIFY_CHAT_API_KEY=在这里填你的对话密钥
DIFY_BASE_URL=https://api.dify.ai

# 服务监听
GIFT_RIGHT_HOST=0.0.0.0
GIFT_RIGHT_PORT=8080

# 访问口令：朋友用 http://IP:8080/?token=这个值 进入
# 改成一个别人猜不到的字符串
GIFT_RIGHT_ACCESS_TOKEN=改成你的口令

# 每分钟每个 IP 最多几次推荐/对话
GIFT_RIGHT_RATE_LIMIT=6
ENVEOF
    chmod 600 "$APP_DIR/.env"
    echo "已生成 $APP_DIR/.env —— 需要你填密钥"
else
    echo "$APP_DIR/.env 已存在，保留不覆盖"
fi

# 加载环境配置（供脚本后续健康检查、防火墙放行及完成提示使用）
if [ -f "$APP_DIR/.env" ]; then
    set -a
    . "$APP_DIR/.env"
    set +a
fi
GIFT_RIGHT_PORT=${GIFT_RIGHT_PORT:-8080}

echo
echo "=== 4. 设置开机自启 + 崩溃保活 ==="
# 2026-11-18：这一步必须在「启动」之前做。
# 原来的顺序是先 nohup 起一个、再用 systemd 起一个，后者撞上
# OSError: [Errno 98] Address already in use 退出，而 Restart=always
# 让它每 5 秒重试一次，实测累计重启 268 次才等到 nohup 那个进程退出。
# 现在只留 systemd 一个管理者。
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    USE_SYSTEMD=1
    cat > /etc/systemd/system/giftright.service <<SVCEOF
[Unit]
Description=GiftRight
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=/usr/bin/env $PY -X utf8 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
    systemctl daemon-reload
    systemctl enable giftright >/dev/null 2>&1
    echo "已配置 systemd（开机自启 + 崩溃自动重启）"
else
    USE_SYSTEMD=0
    echo "非 root 或无 systemd，改用 crontab 保活"
    (crontab -l 2>/dev/null | grep -v 'giftright-keepalive'; \
     echo "* * * * * pgrep -f 'python.*app.py' > /dev/null || (cd $APP_DIR && set -a && . ./.env && set +a && nohup $PY -X utf8 app.py >> $APP_DIR/giftright.log 2>&1 &) # giftright-keepalive") | crontab -
    echo "已加 crontab 保活（每分钟检查一次）"
fi

echo
echo "=== 5. 启动服务 ==="
# 先彻底停掉所有旧进程，再交给唯一的管理者启动，避免端口冲突
if [ "$USE_SYSTEMD" -eq 1 ]; then
    systemctl stop giftright >/dev/null 2>&1 || true
fi
pkill -f "python.*app.py" 2>/dev/null || true
sleep 2
# pkill 之后再确认一次，避免僵尸占端口
pkill -9 -f "python.*app.py" 2>/dev/null || true
sleep 1

if [ "$USE_SYSTEMD" -eq 1 ]; then
    systemctl reset-failed giftright >/dev/null 2>&1 || true
    systemctl start giftright
    echo "已由 systemd 启动（日志进 journalctl -u giftright）"
else
    set -a
    . "$APP_DIR/.env"
    set +a
    cd "$APP_DIR"
    nohup $PY -X utf8 app.py > "$APP_DIR/giftright.log" 2>&1 &
    echo "已启动，PID $!"
fi

echo
echo "=== 6. 等待就绪 ==="
for i in $(seq 1 30); do
    sleep 1
    if curl -s -m 2 "http://127.0.0.1:${GIFT_RIGHT_PORT}/api/health" | grep -q '"ok"'; then
        echo "服务已就绪（${i}秒）"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "30 秒未就绪，最后 20 行日志："
        if [ "$USE_SYSTEMD" -eq 1 ]; then
            journalctl -u giftright -n 20 --no-pager
        else
            tail -20 "$APP_DIR/giftright.log"
        fi
    fi
done

echo
echo "=== 7. 健康检查 ==="
HEALTH=$(curl -s "http://127.0.0.1:${GIFT_RIGHT_PORT}/api/health" || echo "")
echo "${HEALTH:-（无响应）}"
# 密钥还是占位文字的话，这里必须说清楚，不能让人以为配好了。
case "$HEALTH" in
    *'"configured": true'*) echo "Dify 密钥已配置" ;;
    *)
        echo
        echo "!! Dify 密钥没配好，AI 精选不会工作。"
        echo "!! 去 Dify 后台该应用的「访问 API」页拿 app- 开头的密钥，然后："
        echo "!!   sed -i 's|^DIFY_API_KEY=.*|DIFY_API_KEY=app-你的密钥|' $APP_DIR/.env"
        echo "!!   sed -i 's|^DIFY_CHAT_API_KEY=.*|DIFY_CHAT_API_KEY=app-你的密钥|' $APP_DIR/.env"
        echo "!!   systemctl restart giftright"
        ;;
esac

echo
echo "=== 8. 放行系统防火墙 ==="
# 安全组放行之后，机器自己的 firewalld 还可能在拦。两处都要开。
if command -v firewall-cmd >/dev/null 2>&1; then
    if firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port=${GIFT_RIGHT_PORT}/tcp >/dev/null 2>&1 \
            && firewall-cmd --reload >/dev/null 2>&1 \
            && echo "已放行 firewalld 端口 ${GIFT_RIGHT_PORT}" \
            || echo "firewalld 放行失败（可手动：firewall-cmd --add-port=${GIFT_RIGHT_PORT}/tcp --permanent && firewall-cmd --reload）"
    else
        echo "firewalld 未运行，跳过"
    fi
else
    echo "无 firewalld，跳过"
fi

echo
echo "=========================================="
echo "  部署完成"
echo "=========================================="
PUBLIC_IP=$(curl -s -m 5 ifconfig.me 2>/dev/null || curl -s -m 5 ip.sb 2>/dev/null || echo "你的公网IP")
echo
echo "  朋友访问地址："
echo "  http://${PUBLIC_IP}:${GIFT_RIGHT_PORT}/?token=${GIFT_RIGHT_ACCESS_TOKEN}"
echo
echo "  你自己的管理地址："
echo "  http://${PUBLIC_IP}:${GIFT_RIGHT_PORT}/api/health"
echo
echo "  重要：先去安全组开放 ${GIFT_RIGHT_PORT} 端口，否则外面连不上"
echo "  日志：tail -f $APP_DIR/giftright.log"
echo "=========================================="
