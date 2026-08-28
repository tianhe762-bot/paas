"""网页端"更新与卸载"：可选一键维护（需挂载 Docker socket + 项目目录）。

默认不启用一键模式；启用方式见 compose.maintenance.yaml 与 README。
不可用时，管理面板展示手动命令。
"""

import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("paas.maintenance")

SOCKET = Path("/var/run/docker.sock")
HOST_PROJECT = Path("/host-paas")
HOST_DATA = Path("/host-paas-data")
HELPER_IMAGE = "docker:cli"
UNINSTALL_ALL_CONFIRM = "删除全部数据"
ACTIONS = {"update", "uninstall_keep", "uninstall_all"}


def host_project_dir() -> str:
    return os.environ.get("PAAS_HOST_PROJECT", "/opt/paas").rstrip("/")


def maintenance_available() -> tuple[bool, str]:
    if os.environ.get("PAAS_MAINTENANCE", "0") != "1":
        return False, "未启用一键维护模式（需使用 compose.maintenance.yaml 覆盖启动）"
    if not SOCKET.exists():
        return False, "未挂载 Docker socket（/var/run/docker.sock）"
    if not (HOST_PROJECT / "compose.yaml").exists():
        return False, "未挂载项目目录（/host-paas）"
    return True, ""


def build_command(action: str) -> list[str]:
    if action == "update":
        return ["docker", "compose", "-f", "/host-paas/compose.yaml", "up", "-d", "--build"]
    if action == "uninstall_keep":
        return ["docker", "compose", "-f", "/host-paas/compose.yaml", "down"]
    if action == "uninstall_all":
        return [
            "sh", "-c",
            "docker compose -f /host-paas/compose.yaml down; "
            "rm -rf /host-paas-data/*; docker rmi paas:latest || true",
        ]
    raise ValueError(f"未知操作: {action}")


def commands_text() -> dict[str, str]:
    proj = host_project_dir()
    return {
        "update": f"cd {proj} && git pull --ff-only && docker compose up -d --build",
        "uninstall_keep": f"cd {proj} && docker compose down   # 停止并移除容器，保留 {proj}/data 数据",
        "uninstall_all": (
            f"cd {proj} && docker compose down && rm -rf {proj}/data {proj}/logs "
            "&& docker rmi paas:latest"
            "   # 停止并删除容器、镜像与全部数据，不可恢复"
        ),
    }


async def _docker(method: str, path: str, json_body: dict | None = None):
    transport = httpx.AsyncHTTPTransport(uds=str(SOCKET), timeout=60.0)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.request(method, "http://docker" + path, json=json_body)
        if resp.status_code >= 400:
            raise RuntimeError(f"Docker API {method} {path} -> {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}


async def run_maintenance(action: str, confirm: str) -> dict:
    if action not in ACTIONS:
        return {"ok": False, "error": "未知操作"}
    ok, reason = maintenance_available()
    if not ok:
        return {"ok": False, "error": reason}
    if action == "uninstall_all" and confirm.strip() != UNINSTALL_ALL_CONFIRM:
        return {
            "ok": False,
            "error": f"删除全部数据需要输入确认短语「{UNINSTALL_ALL_CONFIRM}」",
        }
    cmd = build_command(action)
    name = f"paas-maintain-{int(time.time())}"
    project = host_project_dir()
    config = {
        "Image": HELPER_IMAGE,
        "Cmd": cmd,
        "WorkingDir": "/host-paas",
        "HostConfig": {
            "Binds": [
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{project}:/host-paas:ro",
                f"{project}/data:/host-paas-data",
            ],
            "NetworkMode": "host",
            "AutoRemove": True,
        },
    }
    try:
        created = await _docker("POST", f"/containers/create?name={name}", json_body=config)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动维护容器失败：{exc}"}
    cid = created.get("Id", "")
    if not cid:
        return {"ok": False, "error": "未获取到维护容器 ID"}
    try:
        await _docker("POST", f"/containers/{cid}/start")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动维护容器失败：{exc}"}
    log.warning("维护操作已发起: %s (container=%s)", action, name)
    return {
        "ok": True,
        "message": (
            f"已发起「{action}」，面板可能暂时不可用。"
            f"查看进度：docker logs -f {name}；本机日志：logs/maintenance.log"
        ),
    }

