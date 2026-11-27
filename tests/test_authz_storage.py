"""台账的存放位置（评审 I1）。

台账不能落在 agent 可写的目录里。`data/` 是可写区 —— 台账放在那儿的话，
agent 自己就能往 `auth_grants` 里塞一条授权，给自己签通行证。

这两条测试只问「台账落在哪个文件里」，不假设它应该搬到哪 ——
位置是实现细节，性质才是要钉住的东西。
"""

import sqlite3

import pytest

from src.agent import paths, session_manager


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """复现真实布局：工作区里有可写区 `data/`，会话库就落在里面。"""
    writable = tmp_path / "data"
    writable.mkdir()
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", writable / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    return tmp_path


def test_the_authorization_ledger_is_not_in_the_agent_writable_zone(layout):
    """台账所在文件不能落在 tmp/ 或 data/ 里。"""
    from src.agent.authz import ledger

    # 台账落在哪个文件里 —— 从连接自己问，别猜
    ledger_file = ledger._conn().execute("PRAGMA database_list").fetchone()[2]
    assert ledger_file

    # 可写区 = agent 写得了的地方。台账落在那儿，agent 就能自己改台账。
    assert not paths.is_writable_path(ledger_file)


def test_writing_a_grant_into_the_session_db_does_not_authorize_anything(layout):
    """agent 往会话库里塞一条授权 —— 台账不读那个文件，所以什么也没发生。"""
    from src.agent.authz import ledger

    conn = session_manager._get_conn()
    try:
        conn.execute(
            "INSERT INTO auth_grants (id, pattern, tool, scope, created_at, session_id) "
            "VALUES ('x', 'bash:rm -rf /', 'bash', 'persistent', 0, NULL)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 表已经不在会话库里了 —— 更好

    assert ledger.match_grant("bash", {"command": "rm -rf /"}, session_id=None) is None


def test_the_audit_log_is_not_in_the_agent_writable_zone(layout):
    """审计日志也不能落在 agent 写得了的地方 —— 否则 agent 能改自己的审计记录。

    台账（authz.db）和审计日志（authz.log）是一对：一个是「授权凭据」，
    一个是「授权经过」。两个都得在 agent 的手外面。
    """
    from src.agent import tools
    from src.agent.authz import audit

    log_file = audit.authz_log_path()
    assert log_file
    # 在工作区里（否则这条测试跟工作区无关）
    assert paths.resolve_in_workdir(str(log_file)) is not None
    assert not paths.is_writable_path(str(log_file))
    # bash 往那儿写也要按只读区拦下来
    assert tools.bash_blocked_write_target("echo x > .labpilot/authz.log")
