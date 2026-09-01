#!/usr/bin/env python3
"""Opt-in real API smoke test for SiliconFlow function calling.

This script reads the existing key from macOS Keychain through an anonymous
pipe, makes one small local-time tool request, and prints only a safe capability
classification.  It never prints or writes the credential or model response.
It is not part of automatic unit-test discovery.
"""

import getpass
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app  # noqa: E402


KEYCHAIN_SERVICE = "Smart Speaker Demo - SiliconFlow API Key"


def main() -> int:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                getpass.getuser(),
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print("FUNCTION_CALLING_NOT_TESTED keychain_key_unavailable")
        return 2
    key = completed.stdout.strip()
    if not key:
        print("FUNCTION_CALLING_NOT_TESTED keychain_key_empty")
        return 2
    try:
        config = app.AppConfig(
            provider_id="siliconflow",
            api_key=key,
            api_base="https://api.siliconflow.cn/v1",
            text_model="Qwen/Qwen3.5-4B",
            request_timeout=60,
        )
        turn = app.SiliconFlowHTTPClient(config).respond_with_tools(
            "请使用工具告诉我这台设备现在的本地时间。",
            [],
            app.ToolRouter(),
        )
        if turn.tools_used == ["get_local_time"]:
            print("FUNCTION_CALLING_OK model_invoked_get_local_time")
            return 0
        if any("local_fallback" in name for name in turn.tools_used):
            print("FUNCTION_CALLING_FALLBACK deterministic_local_router_used")
            return 0
        print("FUNCTION_CALLING_NO_TOOL model_returned_plain_text")
        return 1
    except app.UpstreamAPIError as exc:
        print(
            "FUNCTION_CALLING_API_ERROR status=%d stage=%s"
            % (exc.status, exc.stage)
        )
        return 1
    finally:
        del key


if __name__ == "__main__":
    raise SystemExit(main())
