"""授权台账的测试。

台账记的是**用户批准过的操作范围**。窄模式（精确到某个目录/脚本/接口）
持久化到 `auth_grants` 表，宽模式只留在会话内存里 —— 宽模式会扩大到用户
当初没同意过的操作。
"""

import pytest

from src.agent import session_manager
from src.agent.authz import ledger


@pytest.fixture
def db(tmp_path, monkeypatch):
    """把 SQLite 隔离到临时文件。"""
    from src.agent import paths

    monkeypatch.setattr(session_manager, "SQLITE_DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(session_manager, "_sql_conn", None)
    # 台账跟着工作区走 —— 指到临时目录，测试不该往用户的库里写
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    ledger.reset_session_grants()
    yield
    ledger.reset_session_grants()
    monkeypatch.setattr(session_manager, "_sql_conn", None)


def test_narrow_grant_persists_and_matches(db):
    ledger.record_grant("write_file:tmp/exp1/**")
    grants = ledger.list_grants()
    assert len(grants) == 1
    assert grants[0]["pattern"] == "write_file:tmp/exp1/**"
    assert grants[0]["scope"] == "persistent"


def test_wide_grant_stays_in_session_only(db):
    ledger.record_grant("bash:*", session_id="s1")
    assert ledger.list_grants(scope="persistent") == []
    assert ledger.list_grants(session_id="s1")
    assert ledger.list_grants(scope="persistent", session_id="s1") == []


def test_grant_matches_write_file_by_path_prefix(db):
    ledger.record_grant("write_file:tmp/exp1/**")
    assert ledger.match_grant("write_file", {"path": "tmp/exp1/a.txt"})
    assert ledger.match_grant("write_file", {"path": "tmp/exp1/sub/b.txt"})
    assert not ledger.match_grant("write_file", {"path": "tmp/other/a.txt"})
    assert not ledger.match_grant("write_file", {"path": "src/x.py"})


# ==================== 模式的前缀边界（评审 M2）====================
# 「允许并记住」要真的管用：记住了基址，下一条子路径就该命中。
# 但也不能退化成裸前缀 —— 端口不同、路径不同的东西不能顺手放行。


def test_a_remembered_url_grant_covers_the_sub_path(db):
    ledger.record_grant("bash:curl http://127.0.0.1:8001")
    assert ledger.match_grant("bash", {"command": "curl http://127.0.0.1:8001/run"})


def test_a_remembered_url_grant_does_not_cover_a_different_port(db):
    ledger.record_grant("bash:curl http://127.0.0.1:8001")
    assert not ledger.match_grant("bash", {"command": "curl http://127.0.0.1:80011/run"})


def test_a_remembered_url_grant_does_not_cover_a_different_host(db):
    ledger.record_grant("bash:curl http://127.0.0.1:8001")
    assert not ledger.match_grant(
        "bash", {"command": "curl http://127.0.0.1:8001.evil.com/run"}
    )


def test_a_remembered_path_grant_does_not_cover_a_sibling_with_the_same_prefix(db):
    ledger.record_grant("bash:cat tmp/a")
    assert not ledger.match_grant("bash", {"command": "cat tmp/ab"})


def test_a_remembered_path_grant_covers_a_path_below_it(db):
    ledger.record_grant("bash:cat tmp/a")
    assert ledger.match_grant("bash", {"command": "cat tmp/a/b.txt"})


def test_grant_matches_bash_by_exe_and_path(db):
    ledger.record_grant("bash:python tmp/analyze.py")
    assert ledger.match_grant("bash", {"command": "python tmp/analyze.py --x 1"})
    assert not ledger.match_grant("bash", {"command": "python tmp/other.py"})
    assert not ledger.match_grant("bash", {"command": "rm tmp/analyze.py"})


def test_grant_matches_bash_curl_exact_url(db):
    ledger.record_grant("bash:curl http://127.0.0.1:8001/lock/manual")
    assert ledger.match_grant(
        "bash", {"command": "curl http://127.0.0.1:8001/lock/manual"}
    )
    assert not ledger.match_grant(
        "bash", {"command": "curl http://127.0.0.1:8001/lock/other"}
    )


def test_grant_does_not_match_other_tool(db):
    ledger.record_grant("write_file:tmp/exp1/**")
    assert not ledger.match_grant("edit_file", {"path": "tmp/exp1/a.txt"})


def test_revoke_removes_grant(db):
    ledger.record_grant("write_file:tmp/exp1/**")
    grants = ledger.list_grants()
    assert ledger.revoke_grant(grants[0]["id"])
    assert ledger.list_grants() == []
    assert not ledger.match_grant("write_file", {"path": "tmp/exp1/a.txt"})
    assert not ledger.revoke_grant("no-such-id")


def test_persistent_grant_visible_without_session_id(db):
    ledger.record_grant("write_file:tmp/exp1/**", session_id="s1")
    assert ledger.match_grant("write_file", {"path": "tmp/exp1/a.txt"})
    assert ledger.match_grant("write_file", {"path": "tmp/exp1/a.txt"}, session_id="other")


def test_session_grant_not_visible_from_other_session(db):
    ledger.record_grant("bash:*", session_id="s1")
    assert ledger.match_grant("bash", {"command": "anything"}, session_id="s1")
    assert not ledger.match_grant("bash", {"command": "anything"}, session_id="s2")


def test_clear_session_grants(db):
    ledger.record_grant("bash:*", session_id="s1")
    ledger.clear_session_grants("s1")
    assert ledger.list_grants(session_id="s1") == []


def test_compound_bash_command_does_not_match_single_segment_grant(db):
    ledger.record_grant("bash:python tmp/analyze.py")
    assert not ledger.match_grant("bash", {"command": "python tmp/analyze.py && rm -rf data"})
