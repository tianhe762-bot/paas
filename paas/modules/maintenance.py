"""网页端"更新与卸载"：直接执行（默认可用，无需模式开关）。

compose.yaml 默认挂载宿主机 Docker socket 与项目目录、容器以 root 运行；
paas 镜像内置 docker CLI + compose 插件，维护操作在独立的辅助容器中执行，
避免更新重建时打断自身。
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
HELPER_IMAGE = "paas:latest"
UNINSTALL_ALL_CONFIRM = "删除全部数据"
ACTIONS = {"update", "uninstall_keep", "uninstall_all"}


def host_project_dir() -> str:
    return os.environ.get("PAAS_HOST_PROJECT", "/opt/paas").rstrip("/")


def maintenance_available() -> tuple[bool, str]:
    if not SOCKET.exists():
        return False, "未挂载 Docker socket（/var/run/docker.sock）"
    if not (HOST_PROJECT / "compose.yaml").exists():
        return False, "未挂载项目目录（/host-paas）"
    return True, ""


def build_command(action: str) -> list[str]:
    if action == "update":
        return ["docker-compose", "-f", "/host-paas/compose.yaml", "up", "-d", "--build"]
    if action == "uninstall_keep":
        return ["docker-compose", "-f", "/host-paas/compose.yaml", "down"]
    if action == "uninstall_all":
        return [
            "sh", "-c",
            "docker-compose -f /host-paas/compose.yaml down --rmi local; "
            "rm -rf /host-paas-data/*",
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
    # httpx>=0.28 的 timeout 在客户端上设置，transport 不再接受 timeout 参数
    transport = httpx.AsyncHTTPTransport(uds=str(SOCKET))
    async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
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
        "User": "root",
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
