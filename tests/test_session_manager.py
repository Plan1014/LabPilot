"""会话库：写消息时不该因为会话行不存在而崩。"""

import src.agent.session_manager as session_manager


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)


def test_append_to_session_survives_unknown_session(tmp_path, monkeypatch):
    """用不存在的 session_id 发消息时，末尾不该抛 TypeError。

    会话行不存在 → UPDATE 不生效 → get_session_meta 返回 None。
    """
    _isolate(tmp_path, monkeypatch)

    count = session_manager.append_to_session(
        "no-such-session", [{"role": "user", "content": "你好"}]
    )

    assert count == 0


def test_append_to_session_counts_for_existing_session(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session_id = session_manager.create_session()

    session_manager.append_to_session(session_id, [{"role": "user", "content": "你好"}])
    count = session_manager.append_to_session(session_id, [{"role": "user", "content": "再问"}])

    assert count == 2
