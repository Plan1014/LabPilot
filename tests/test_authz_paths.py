"""路径区域策略的测试 —— 可写区 tmp/data 与只读保护区。

这些规则是整个鉴权层的地基：闸门层、bash 工具都靠它判断「这个路径能不能写」。
"""

import pytest

from src.agent import paths


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """把 paths 的 WORKDIR 指向临时目录，并铺出受保护目录与可写区。"""
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    (tmp_path / "src" / "agent").mkdir(parents=True)
    (tmp_path / "skills").mkdir()
    (tmp_path / "tmp").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    return tmp_path


def test_tmp_and_data_are_writable(workdir):
    assert paths.is_writable_path("tmp/a.txt")
    assert paths.is_writable_path("data/x/y.json")
    assert paths.classify_target("tmp/a.txt") == "writable"
    assert paths.classify_target("data/x/y.json") == "writable"


def test_src_is_not_writable_and_is_protected(workdir):
    assert not paths.is_writable_path("src/agent/tools.py")
    assert paths.is_protected_path("src/agent/tools.py")
    assert paths.classify_target("src/agent/tools.py") == "repo_readonly"


def test_frontend_src_is_protected(workdir):
    assert paths.is_protected_path("frontend/src/App.tsx")
    assert not paths.is_writable_path("frontend/src/App.tsx")


def test_env_file_is_protected_root_file(workdir):
    assert paths.is_protected_path(".env")
    assert not paths.is_writable_path(".env")
    assert paths.classify_target(".env") == "repo_readonly"


def test_path_escaping_workdir_resolves_to_none(workdir):
    assert paths.resolve_in_workdir("../outside.txt") is None
    assert paths.resolve_in_workdir("C:/somewhere/else.txt") is None
    assert paths.classify_target("../outside.txt") == "outside"


def test_classify_target_distinguishes_outside_from_readonly(workdir):
    assert paths.classify_target("src/a.py") == "repo_readonly"
    assert paths.classify_target("../outside.txt") == "outside"


def test_repo_root_file_is_readonly_not_writable(workdir):
    assert paths.classify_target("README.md") == "repo_readonly"
    assert paths.is_protected_path("README.md")


def test_unparseable_path_is_not_writable(workdir):
    # 解析失败 → 一律不可写（fail-closed）
    assert paths.is_writable_path("../outside.txt") is False
    assert paths.is_protected_path("../outside.txt") is False


def test_resolve_in_workdir_returns_absolute_path(workdir):
    p = paths.resolve_in_workdir("tmp/a.txt")
    assert p is not None
    assert p == (workdir / "tmp" / "a.txt").resolve()
