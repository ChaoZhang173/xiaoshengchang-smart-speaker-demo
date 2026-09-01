#!/bin/zsh
set -e

KEYCHAIN_ACCOUNT="$(id -un)"
KEYCHAIN_SERVICE="Smart Speaker Demo - OpenAI API Key"

echo ""
echo "智能音箱 Demo · 可选配置 OpenAI API Key"
echo "Key 只会保存在这台 Mac 的钥匙串中，不会写进代码或网页。"
echo "请不要把 API Key 发到聊天里。"
echo "接下来 macOS 会安全地提示你输入 Key。输入时屏幕不会显示字符。"
echo ""
/usr/bin/security add-generic-password \
  -U \
  -a "$KEYCHAIN_ACCOUNT" \
  -s "$KEYCHAIN_SERVICE" \
  -w

echo ""
echo "API Key 已安全保存。"
echo "现在可以双击 start.command 启动真实 API 模式。"
read -k 1 "?按任意键关闭…"
echo ""
