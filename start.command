#!/bin/zsh
set -e

DEMO_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
KEYCHAIN_ACCOUNT="$(id -un)"
SILICONFLOW_KEYCHAIN_SERVICE="Smart Speaker Demo - SiliconFlow API Key"
OPENAI_KEYCHAIN_SERVICE="Smart Speaker Demo - OpenAI API Key"
DEMO_PORT="${SMART_SPEAKER_PORT:-8765}"
EXPECTED_BUILD_ID="agent-foundation-v1"
OPEN_BROWSER=1
FORCE_MOCK=0
PYTHON_BIN="/usr/bin/python3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

if [[ -z "$PYTHON_BIN" ]] || [[ ! -x "$PYTHON_BIN" ]]; then
  echo ""
  echo "启动失败：这台 Mac 没有找到 Python 3。"
  echo "请先安装 Python 3.9 或更高版本，然后重新双击 start.command。"
  echo "也可以请 Codex 阅读同目录的 CODEX_HELP.md 协助检查。"
  echo ""
  if [[ -t 0 ]]; then
    read -k 1 "?按任意键关闭…"
    echo ""
  fi
  exit 2
fi

if ! "$PYTHON_BIN" -c \
  'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  >/dev/null 2>&1; then
  PYTHON_VERSION="$($PYTHON_BIN --version 2>&1 || true)"
  echo ""
  echo "启动失败：需要 Python 3.9 或更高版本。"
  echo "当前检测到：${PYTHON_VERSION:-未知版本}"
  echo "请升级 Python 后重新双击 start.command。"
  echo ""
  if [[ -t 0 ]]; then
    read -k 1 "?按任意键关闭…"
    echo ""
  fi
  exit 2
fi

for DEMO_ARG in "$@"; do
  if [[ "$DEMO_ARG" == "--no-browser" ]]; then
    OPEN_BROWSER=0
  elif [[ "$DEMO_ARG" == "--mock" ]]; then
    FORCE_MOCK=1
  fi
done

# Check whether credentials exist without reading their values. Only the Key
# for the selected provider is loaded into the local server process below.
HAS_SILICONFLOW_KEY=0
HAS_OPENAI_KEY=0
if [[ -n "${SILICONFLOW_API_KEY:-}" ]] || \
  /usr/bin/security find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" -s "$SILICONFLOW_KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  HAS_SILICONFLOW_KEY=1
fi
if [[ -n "${OPENAI_API_KEY:-}" ]] || \
  /usr/bin/security find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" -s "$OPENAI_KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  HAS_OPENAI_KEY=1
fi

if (( FORCE_MOCK )); then
  EXPECTED_PROVIDER="mock"
else
  case "${SMART_SPEAKER_MODE:-auto}" in
    siliconflow) EXPECTED_PROVIDER="siliconflow" ;;
    openai) EXPECTED_PROVIDER="openai" ;;
    mock) EXPECTED_PROVIDER="mock" ;;
    auto)
      if (( HAS_SILICONFLOW_KEY )); then
        EXPECTED_PROVIDER="siliconflow"
      else
        EXPECTED_PROVIDER="mock"
      fi
      ;;
    *) EXPECTED_PROVIDER="invalid" ;;
  esac
fi

if [[ "${SMART_SPEAKER_MODE:-auto}" == "auto" ]] && \
  (( ! HAS_SILICONFLOW_KEY && HAS_OPENAI_KEY )); then
  echo "检测到旧的 OpenAI Key，但没有找到 SiliconFlow Key。"
  echo "为避免误用其他服务，本次只进入本地演示；请先双击 configure_api_key.command。"
fi

if [[ "$EXPECTED_PROVIDER" == "siliconflow" && -z "${SILICONFLOW_API_KEY:-}" ]]; then
  SELECTED_API_KEY="$(/usr/bin/security find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" \
    -s "$SILICONFLOW_KEYCHAIN_SERVICE" \
    -w 2>/dev/null || true)"
  if [[ -n "$SELECTED_API_KEY" ]]; then
    export SILICONFLOW_API_KEY="$SELECTED_API_KEY"
  fi
  unset SELECTED_API_KEY
elif [[ "$EXPECTED_PROVIDER" == "openai" && -z "${OPENAI_API_KEY:-}" ]]; then
  SELECTED_API_KEY="$(/usr/bin/security find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" \
    -s "$OPENAI_KEYCHAIN_SERVICE" \
    -w 2>/dev/null || true)"
  if [[ -n "$SELECTED_API_KEY" ]]; then
    export OPENAI_API_KEY="$SELECTED_API_KEY"
  fi
  unset SELECTED_API_KEY
fi

if [[ "$DEMO_PORT" != <-> ]] || (( DEMO_PORT > 65535 )); then
  echo "启动失败：端口必须是 0 到 65535 之间的数字。"
  exit 2
fi

if (( DEMO_PORT > 0 )); then
  DEMO_URL="http://127.0.0.1:${DEMO_PORT}"

  # Repeated double-clicks should focus the running demo instead of failing.
  RUNNING_HEALTH="$(/usr/bin/curl -fsS --max-time 1 "$DEMO_URL/api/health" 2>/dev/null || true)"
  if [[ -n "$RUNNING_HEALTH" ]] && \
    /usr/bin/curl -fsS --max-time 1 "$DEMO_URL/" 2>/dev/null | \
      /usr/bin/grep -q "小声场 · 智能音箱 Demo"; then
    if print -r -- "$RUNNING_HEALTH" | \
      /usr/bin/grep -q "\"provider_id\": \"${EXPECTED_PROVIDER}\"" && \
      print -r -- "$RUNNING_HEALTH" | \
        /usr/bin/grep -q "\"build_id\": \"${EXPECTED_BUILD_ID}\""; then
      if (( OPEN_BROWSER )); then
        echo "相同模式的 Demo 已经在运行，正在打开浏览器：${DEMO_URL}"
        /usr/bin/open "$DEMO_URL"
      else
        echo "相同模式的 Demo 已经在运行：${DEMO_URL}"
      fi
      exit 0
    else
      echo "端口 ${DEMO_PORT} 上有另一种模式或旧版本 Demo，将为新版自动选择可用端口。"
      DEMO_PORT=0
    fi
  fi
  unset RUNNING_HEALTH

  # If another application owns 8765, let macOS select an available port.
  if (( DEMO_PORT > 0 )) && \
    /usr/sbin/lsof -nP "-iTCP:${DEMO_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "端口 ${DEMO_PORT} 正被其他程序使用，将自动选择可用端口。"
    DEMO_PORT=0
  fi
fi

cd "$DEMO_DIR"
exec "$PYTHON_BIN" "$DEMO_DIR/app.py" --port "$DEMO_PORT" "$@"
