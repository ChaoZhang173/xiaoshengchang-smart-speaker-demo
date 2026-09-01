#!/bin/zsh
set -e

KEYCHAIN_ACCOUNT="$(id -un)"
KEYCHAIN_SERVICE="Smart Speaker Demo - SiliconFlow API Key"

echo ""
echo "智能音箱 Demo · 配置 SiliconFlow（硅基流动）API Key"
echo "Key 只会保存在这台 Mac 的钥匙串中，不会写进代码或网页。"
echo "请不要把 API Key 发到聊天里。"
echo ""
echo "接下来 macOS 会安全地提示你输入密码："
echo "请粘贴 SiliconFlow API Key，然后按回车。输入时屏幕不会显示字符。"
echo ""

# Putting -w last makes the security tool prompt securely instead of exposing
# the credential in this script's command-line arguments.
if ! /usr/bin/security add-generic-password \
  -U \
  -a "$KEYCHAIN_ACCOUNT" \
  -s "$KEYCHAIN_SERVICE" \
  -w; then
  echo ""
  echo "保存失败：macOS 钥匙串没有接受这次输入。请重新运行本脚本。"
  read -k 1 "?按任意键关闭…"
  echo ""
  exit 1
fi

if ! /usr/bin/security find-generic-password \
  -a "$KEYCHAIN_ACCOUNT" \
  -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  echo ""
  echo "保存后检查失败：没有找到 SiliconFlow 钥匙串记录。"
  read -k 1 "?按任意键关闭…"
  echo ""
  exit 1
fi

echo ""
echo "API Key 已安全保存，并通过钥匙串自检。"
echo "现在双击 start.command，即会优先使用 SiliconFlow 免费模型。"
read -k 1 "?按任意键关闭…"
echo ""
