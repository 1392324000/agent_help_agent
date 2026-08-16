"""
公网网络工具
============
检测本机公网 IP（多 API 轮询 + 缓存），用于：
  - Hub 启动时打印公网访问地址
  - Agent 注册时 endpoint 自动公网化（"auto" 占位符）
  - info 接口返回公网 hub_url

公网 IP 探测 API（按序轮询，任一成功即返回）：
  https://api.ipify.org / https://ifconfig.me/ip / https://checkip.amazonaws.com
环境变量 AGENT_PUBLIC_IP 可显式指定（绕过探测）。
"""

from __future__ import annotations

import os
import threading
import urllib.request

_PUBLIC_IP_API = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com",
]
_USER_AGENT = "agent-marketplace/1.0"
_cache: dict[str, str] = {}          # url -> ip
_lock = threading.Lock()


def fetch_public_ip(timeout: float = 5.0) -> str | None:
    """探测本机公网 IPv4。多端点轮询，命中即返回；全部失败返回 None。"""
    for api in _PUBLIC_IP_API:
        if api in _cache:
            return _cache[api]
        try:
            req = urllib.request.Request(api, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ip = resp.read().decode("utf-8").strip()
            # 校验是 IPv4
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                with _lock:
                    _cache[api] = ip
                return ip
        except Exception:
            continue
    return None


def public_ip(timeout: float = 5.0) -> str | None:
    """获取公网 IP：环境变量 AGENT_PUBLIC_IP 优先，其次探测。"""
    env = os.environ.get("AGENT_PUBLIC_IP", "").strip()
    if env:
        return env
    return fetch_public_ip(timeout)


def public_endpoint(port: int, path: str = "", timeout: float = 5.0) -> str:
    """生成公网地址：http://<公网IP>:<port>/<path>。探测失败回退本机地址。"""
    ip = public_ip(timeout)
    if ip:
        return f"http://{ip}:{port}/{path}".rstrip("/")
    return f"http://127.0.0.1:{port}/{path}".rstrip("/")
