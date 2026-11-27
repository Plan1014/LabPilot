"""「允许并记住」模式的推导与窄/宽分界测试。

模式是**用户授权过的操作范围**。窄模式（限定在可写区的具体子树 / 具体脚本 /
具体接口）可以持久化；宽模式（整个 tmp 树、整个服务、外网）只能留在会话里 ——
用户当初批准的是「这一个操作」，不是「这一片」。

模式由 Python 侧确定性推导，鉴权 agent 不参与（spec D8）。
"""

import pytest

from src.agent import paths
from src.agent.authz import patterns


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def test_write_file_pattern_uses_parent_dir(workdir):
    assert patterns.derive_pattern("write_file", {"path": "tmp/exp1/a.txt"}) == "write_file:tmp/exp1/**"


def test_edit_file_pattern_uses_parent_dir(workdir):
    assert patterns.derive_pattern("edit_file", {"path": "tmp/exp1/a.txt"}) == "edit_file:tmp/exp1/**"


def test_bash_pattern_is_exe_plus_path(workdir):
    got = patterns.derive_pattern("bash", {"command": "python tmp/analyze.py --threshold 0.3"})
    assert got == "bash:python tmp/analyze.py"


def test_bash_pattern_for_curl_is_exact_url(workdir):
    got = patterns.derive_pattern("bash", {"command": "curl http://127.0.0.1:8001/pi/calculate"})
    assert got == "bash:curl http://127.0.0.1:8001/pi/calculate"


def test_compound_command_has_no_pattern(workdir):
    assert patterns.derive_pattern("bash", {"command": "cat a && sed -i b c"}) is None


def test_command_without_path_arg_has_no_pattern(workdir):
    assert patterns.derive_pattern("bash", {"command": "ls -la"}) is None


def test_write_outside_writable_zone_has_no_pattern(workdir):
    assert patterns.derive_pattern("write_file", {"path": "src/x.py"}) is None


def test_bash_path_outside_writable_zone_has_no_pattern(workdir):
    assert patterns.derive_pattern("bash", {"command": "python src/analyze.py"}) is None


def test_bash_url_to_unknown_port_has_no_pattern(workdir):
    assert patterns.derive_pattern("bash", {"command": "curl http://127.0.0.1:9999/x"}) is None


def test_bash_url_to_external_host_has_no_pattern(workdir):
    assert patterns.derive_pattern("bash", {"command": "curl https://example.com/x"}) is None


def test_other_tool_has_no_pattern(workdir):
    assert patterns.derive_pattern("read_file", {"path": "src/x.py"}) is None


def test_narrow_vs_wide(workdir):
    assert patterns.is_narrow_pattern("write_file:tmp/exp1/**") is True
    assert patterns.is_narrow_pattern("write_file:tmp/**") is False

    assert patterns.is_narrow_pattern("bash:python tmp/analyze.py") is True
    assert patterns.is_narrow_pattern("bash:python **") is False

    assert patterns.is_narrow_pattern("bash:curl http://127.0.0.1:8001/pi/calculate") is True
    assert patterns.is_narrow_pattern("bash:curl http://127.0.0.1:8001/**") is False
    assert patterns.is_narrow_pattern("bash:curl http://example.com/x") is False
    assert patterns.is_narrow_pattern("bash:*") is False


def test_pattern_outside_writable_zone_is_wide(workdir):
    assert patterns.is_narrow_pattern("write_file:src/exp1/**") is False
