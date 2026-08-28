import pytest


def test_build_commands():
    from paas.modules.maintenance import build_command

    assert build_command("update")[0] == "docker-compose"
    assert "down" in build_command("uninstall_keep")
    assert "rm -rf /host-paas-data/*" in build_command("uninstall_all")[-1]
    with pytest.raises(ValueError):
        build_command("unknown")


def test_commands_text():
    from paas.modules.maintenance import commands_text

    cmds = commands_text()
    assert "docker compose down" in cmds["uninstall_keep"]
    assert "rm -rf" in cmds["uninstall_all"]


def test_available_requires_socket_and_project(tmp_path, monkeypatch):
    from paas.modules import maintenance

    monkeypatch.setattr(maintenance, "SOCKET", tmp_path / "docker.sock")
    monkeypatch.setattr(maintenance, "HOST_PROJECT", tmp_path / "host-paas")
    ok, reason = maintenance.maintenance_available()
    assert not ok
    assert "socket" in reason.lower() or "Docker" in reason
    # 有 socket 但缺少项目 compose.yaml
    (tmp_path / "docker.sock").touch()
    ok, reason = maintenance.maintenance_available()
    assert not ok
    assert "项目目录" in reason
    # socket 与项目都满足
    (tmp_path / "host-paas").mkdir()
    (tmp_path / "host-paas" / "compose.yaml").touch()
    ok, _ = maintenance.maintenance_available()
    assert ok


async def test_run_maintenance_unavailable(tmp_path, monkeypatch):
    from paas.modules import maintenance

    monkeypatch.setattr(maintenance, "SOCKET", tmp_path / "docker.sock")
    r = await maintenance.run_maintenance("update", "")
    assert r["ok"] is False
    assert "socket" in r["error"].lower() or "Docker" in r["error"]


async def test_uninstall_all_requires_confirm(monkeypatch):
    from paas.modules import maintenance

    monkeypatch.setattr(maintenance, "maintenance_available", lambda: (True, ""))
    r = await maintenance.run_maintenance("uninstall_all", "")
    assert r["ok"] is False
    assert "删除全部数据" in r["error"]
    # 确认短语正确时继续执行（无 socket 会走到容器启动失败分支）
    monkeypatch.setattr(maintenance, "maintenance_available", lambda: (True, ""))
    r2 = await maintenance.run_maintenance("uninstall_all", "删除全部数据")
    assert r2["ok"] is False
    assert "启动维护容器失败" in r2["error"] or "Docker" in r2["error"]


async def test_maintenance_api(client):
    await client.post(
        "/admin/api/login", json={"username": "admin", "password": "test-admin-pass-123"}
    )
    status = await client.get("/admin/api/maintenance/status")
    assert status.status_code == 200
    data = status.json()
    assert data["available"] is False
    assert "docker compose" in data["commands"]["update"]
    run = await client.post(
        "/admin/api/maintenance/run", json={"action": "update", "confirm": ""}
    )
    assert run.json()["ok"] is False
