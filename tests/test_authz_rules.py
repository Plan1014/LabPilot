"""闸门层 L0/L1 判定与 skills curl 白名单提取的测试。

闸门层**不重新实现** bash 的命令分析 —— 它调用 `tools.py` 里的辅助函数
（spec D13）。这里验证的是「闸门怎么用那些答案」。
"""

import pytest

from src.agent import paths
from src.agent.authz import rules


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(rules, "SKILLS_DIR", tmp_path / "skills")
    (tmp_path / "src").mkdir()
    (tmp_path / "tmp").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


# ==================== L1：受保护写入 ====================

def test_write_file_to_src_is_denied(workdir):
    reason = rules.find_denied_write_target("write_file", {"path": "src/x.py"})
    assert reason is not None
    assert "src/x.py" in reason


def test_write_file_to_tmp_is_allowed(workdir):
    assert rules.find_denied_write_target("write_file", {"path": "tmp/a.txt"}) is None


def test_write_file_to_data_is_allowed(workdir):
    assert rules.find_denied_write_target("write_file", {"path": "data/a.json"}) is None


def test_edit_file_to_protected_is_denied(workdir):
    reason = rules.find_denied_write_target("edit_file", {"path": "src/x.py"})
    assert reason is not None
    assert "src/x.py" in reason


def test_bash_redirect_to_src_is_denied(workdir):
    reason = rules.find_denied_write_target("bash", {"command": "echo hi > src/x.py"})
    assert reason is not None
    assert "src/x.py" in reason


def test_bash_compound_read_then_write_is_denied_and_names_the_segment(workdir):
    reason = rules.find_denied_write_target(
        "bash", {"command": "cat src/a.py && sed -i 's/a/b/' src/a.py"}
    )
    assert reason is not None
    assert "sed" in reason and "src/a.py" in reason


def test_bash_write_to_tmp_is_not_denied(workdir):
    assert rules.find_denied_write_target("bash", {"command": "echo hi > tmp/a.txt"}) is None


def test_bash_write_outside_workdir_is_not_denied(workdir):
    assert rules.find_denied_write_target("bash", {"command": "echo hi > ../outside.txt"}) is None


def test_other_tools_are_not_denied(workdir):
    assert rules.find_denied_write_target("read_file", {"path": "src/x.py"}) is None


# ==================== L0：白名单 ====================

def test_read_only_tool_is_whitelisted():
    assert rules.is_l0_whitelisted("read_file", {"path": "src/x.py"}) == (True, "read_only_tool")


def test_memory_tool_is_whitelisted():
    assert rules.is_l0_whitelisted("remember_fact", {"fact": "x"}) == (
        True,
        "fixed_path_memory_tool",
    )


def test_write_file_to_tmp_is_whitelisted(workdir):
    ok, rule = rules.is_l0_whitelisted("write_file", {"path": "tmp/a.txt"})
    assert ok and rule == "writable_zone"


def test_bash_is_not_whitelisted_by_default(workdir):
    assert rules.is_l0_whitelisted("bash", {"command": "python tmp/analyze.py"}) == (False, None)


def test_read_only_bash_is_whitelisted(workdir):
    ok, rule = rules.is_l0_whitelisted("bash", {"command": "cat src/foo.py"})
    assert ok and rule == "read_only_bash"


def test_background_bash_is_not_whitelisted(workdir):
    # spec §5.6：background=True 会打穿快照兜底，一律强制进 L2 判官
    ok, _ = rules.is_l0_whitelisted("bash", {"command": "cat src/foo.py", "background": True})
    assert ok is False


# ==================== skills curl ====================

def _write_skill(root, name, body):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def test_extract_skill_curl_prefixes(tmp_path):
    _write_skill(
        tmp_path,
        "demo",
        "调用 `curl.exe -X POST http://127.0.0.1:8001/pi/calculate` 算一下。\n",
    )
    prefixes = rules.extract_skill_curl_prefixes(tmp_path)
    assert "http://127.0.0.1:8001" in prefixes


def test_skill_curl_ignores_non_loopback(tmp_path):
    _write_skill(tmp_path, "demo", "curl https://example.com/x\n")
    assert rules.extract_skill_curl_prefixes(tmp_path) == []


def test_extract_skill_curl_prefixes_tolerates_trailing_punctuation(tmp_path):
    # skill 散文里的句号会粘在端口后面 —— `http://127.0.0.1:8001.` 不是合法端口
    _write_skill(tmp_path, "demo", "见 http://127.0.0.1:8001. 这个接口\n")
    assert "http://127.0.0.1:8001" in rules.extract_skill_curl_prefixes(tmp_path)


def test_extract_skill_curl_prefixes_survives_malformed_port(tmp_path):
    _write_skill(tmp_path, "demo", "见 http://127.0.0.1:notaport/x\n")
    assert rules.extract_skill_curl_prefixes(tmp_path) == []


def test_match_skill_curl_requires_every_segment(tmp_path):
    prefixes = ["http://127.0.0.1:8001"]
    assert rules.match_skill_curl(
        "curl http://127.0.0.1:8001/pi/calculate && curl http://evil.com/x", prefixes
    ) is False


def test_match_skill_curl_tolerates_curl_exe_and_flags(tmp_path):
    prefixes = ["http://127.0.0.1:8001"]
    assert rules.match_skill_curl(
        "curl.exe -s -X POST http://127.0.0.1:8001/pi/calculate", prefixes
    ) is True


def test_match_skill_curl_empty_command_is_false():
    assert rules.match_skill_curl("", ["http://127.0.0.1:8001"]) is False


def test_skill_curl_is_whitelisted(workdir):
    skill = _write_skill(
        workdir / "skills",
        "demo",
        "curl.exe -X POST http://127.0.0.1:8001/pi/calculate\n",
    )
    assert skill.exists()
    ok, rule = rules.is_l0_whitelisted(
        "bash", {"command": "curl -X POST http://127.0.0.1:8001/pi/calculate"}
    )
    assert ok and rule == "skill_curl"
