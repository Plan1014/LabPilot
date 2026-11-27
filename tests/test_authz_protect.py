"""快照兜底（protect.py）的测试。

覆盖：快照、比对、还原、生成物忽略，以及 bash 执行后的兜底接线。
"""

import os
import sys
from pathlib import Path

import pytest

from src.agent import paths
from src.agent.authz import protect


@pytest.fixture
def workdir(tmp_path):
    """造一个带受保护目录的工作区。"""
    (tmp_path / "src" / "agent").mkdir(parents=True)
    (tmp_path / "src" / "agent" / "tools.py").write_text("original", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "s.md").write_text("skill", encoding="utf-8")
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("tsx", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "d.md").write_text("doc", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "tmp").mkdir()
    return tmp_path


# ==================== 快照 ====================

def test_snapshot_copies_protected_dirs(workdir):
    snap = protect.snapshot(workdir)
    assert (snap / "src" / "agent" / "tools.py").read_text(encoding="utf-8") == "original"
    assert (snap / "skills" / "s.md").exists()
    assert (snap / "frontend" / "src" / "App.tsx").exists()
    assert (snap / "docs" / "d.md").exists()
    assert (snap / "README.md").exists()


def test_snapshot_skips_writable_dirs(workdir):
    """tmp/ 是可写区 —— 不该进快照（快照只管受保护区）。"""
    (workdir / "tmp" / "exp").mkdir()
    (workdir / "tmp" / "exp" / "out.txt").write_text("x", encoding="utf-8")
    snap = protect.snapshot(workdir)
    assert not (snap / "tmp").exists()


def test_snapshot_dir_lives_under_tmp_protect(workdir):
    snap = protect.snapshot(workdir)
    assert snap.parent == workdir / "tmp" / "_protect"


# ==================== 比对 ====================

def test_diff_detects_modified_file(workdir):
    snap = protect.snapshot(workdir)
    (workdir / "src" / "agent" / "tools.py").write_text("tampered", encoding="utf-8")
    report = protect.diff_and_restore(snap, workdir)
    assert "src/agent/tools.py" in report["changed"]
    assert report["restored"] is True


def test_diff_detects_added_file(workdir):
    snap = protect.snapshot(workdir)
    (workdir / "src" / "new.py").write_text("new", encoding="utf-8")
    report = protect.diff_and_restore(snap, workdir)
    assert "src/new.py" in report["added"]


def test_diff_detects_deleted_file(workdir):
    snap = protect.snapshot(workdir)
    (workdir / "src" / "agent" / "tools.py").unlink()
    report = protect.diff_and_restore(snap, workdir)
    assert "src/agent/tools.py" in report["removed"]


def test_diff_reports_nothing_when_nothing_changed(workdir):
    snap = protect.snapshot(workdir)
    report = protect.diff_and_restore(snap, workdir)
    assert report["changed"] == []
    assert report["removed"] == []
    assert report["added"] == []
    assert report["restored"] is False


# ==================== 还原 ====================

def test_restore_puts_content_back(workdir):
    snap = protect.snapshot(workdir)
    (workdir / "src" / "agent" / "tools.py").write_text("tampered", encoding="utf-8")
    protect.diff_and_restore(snap, workdir)
    assert (workdir / "src" / "agent" / "tools.py").read_text(encoding="utf-8") == "original"


def test_restore_brings_back_deleted_file(workdir):
    snap = protect.snapshot(workdir)
    (workdir / "src" / "agent" / "tools.py").unlink()
    protect.diff_and_restore(snap, workdir)
    assert (workdir / "src" / "agent" / "tools.py").read_text(encoding="utf-8") == "original"


def test_restore_removes_added_file(workdir):
    snap = protect.snapshot(workdir)
    (workdir / "src" / "new.py").write_text("new", encoding="utf-8")
    protect.diff_and_restore(snap, workdir)
    assert not (workdir / "src" / "new.py").exists()


# ==================== 隔离（不销毁用户文件） ====================

def test_an_added_file_is_quarantined_instead_of_deleted(workdir):
    """受保护区里新出现的文件不能直接删掉 —— 那可能是用户自己写的。

    删了就是销毁用户的劳动成果。改成搬到隔离目录，内容留着。
    """
    snap = protect.snapshot(workdir)
    (workdir / "src" / "new.py").write_text("new", encoding="utf-8")

    report = protect.diff_and_restore(snap, workdir)

    assert not (workdir / "src" / "new.py").exists()
    quarantine = Path(report["quarantine_dir"])
    assert (quarantine / "src" / "new.py").read_text(encoding="utf-8") == "new"
    assert "src/new.py" in report["quarantined"]


def test_a_changed_file_keeps_its_newer_version_in_quarantine(workdir):
    """被改动的文件：现场恢复成快照版本，但用户那份新内容不能消失。"""
    snap = protect.snapshot(workdir)
    (workdir / "src" / "agent" / "tools.py").write_text("tampered", encoding="utf-8")

    report = protect.diff_and_restore(snap, workdir)

    assert (workdir / "src" / "agent" / "tools.py").read_text(encoding="utf-8") == "original"
    quarantine = Path(report["quarantine_dir"])
    assert (quarantine / "src" / "agent" / "tools.py").read_text(encoding="utf-8") == "tampered"
    assert "src/agent/tools.py" in report["quarantined"]


def test_the_report_points_at_the_quarantine_directory(workdir):
    """报告得告诉人文件去哪了 —— 否则等于悄悄弄丢了东西。"""
    snap = protect.snapshot(workdir)
    (workdir / "src" / "new.py").write_text("new", encoding="utf-8")

    report = protect.diff_and_restore(snap, workdir)
    text = protect.format_report(report)

    assert report["quarantine_dir"] in text
    assert "src/new.py" in text


def test_the_quarantine_lives_outside_the_protected_areas(workdir):
    """隔离目录不能在受保护区里 —— 否则下一次比对又把它算成差异。"""
    snap = protect.snapshot(workdir)
    (workdir / "src" / "new.py").write_text("new", encoding="utf-8")

    report = protect.diff_and_restore(snap, workdir)
    quarantine = Path(report["quarantine_dir"])

    assert ".labpilot" in quarantine.parts
    for rel in paths.PROTECTED_DIRS:
        assert not quarantine.is_relative_to(workdir / rel)


def test_nothing_quarantined_when_nothing_changed(workdir):
    snap = protect.snapshot(workdir)
    report = protect.diff_and_restore(snap, workdir)
    assert report["quarantined"] == []
    assert report.get("quarantine_dir") is None


# ==================== 生成物 ====================

def test_generated_artifacts_are_ignored(workdir):
    snap = protect.snapshot(workdir)
    cache = workdir / "src" / "__pycache__"
    cache.mkdir()
    (cache / "tools.cpython-312.pyc").write_bytes(b"\x00")
    report = protect.diff_and_restore(snap, workdir)
    assert report["changed"] == []
    assert report["removed"] == []
    assert report["added"] == []
    assert report["restored"] is False


def test_node_modules_ignored(workdir):
    snap = protect.snapshot(workdir)
    nm = workdir / "src" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("js", encoding="utf-8")
    report = protect.diff_and_restore(snap, workdir)
    assert report["added"] == []
    assert report["restored"] is False


def test_is_generated_artifact(workdir):
    assert protect.is_generated_artifact(workdir / "src" / "__pycache__" / "x.pyc", workdir)
    assert protect.is_generated_artifact(workdir / "src" / "a.pyc", workdir)
    assert protect.is_generated_artifact(workdir / "src" / "x.log", workdir)
    assert protect.is_generated_artifact(workdir / "src" / "pkg.egg-info" / "PKG-INFO", workdir)
    assert not protect.is_generated_artifact(workdir / "src" / "agent" / "tools.py", workdir)


# ==================== bash 执行后的兜底接线 ====================

def test_bash_touching_protected_file_is_restored(monkeypatch, tmp_path):
    """bash 改了 src/ → 快照兜底把它还原，并把这次 bash 判为失败。"""
    from src.agent import graph_thinking, tools

    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "keep.py").write_text("original", encoding="utf-8")
    (work / "tmp").mkdir()
    script = work / "tmp" / "writer.py"
    script.write_text(
        "open('src/keep.py', 'w').write('tampered')\n", encoding="utf-8"
    )

    monkeypatch.setattr(tools, "WORKDIR", work)
    result = graph_thinking.run_single_tool(
        "bash", {"command": f'"{sys.executable}" tmp/writer.py'}
    )

    assert (work / "src" / "keep.py").read_text(encoding="utf-8") == "original"
    assert result.startswith("Error:")
    assert "keep.py" in result


def test_bash_not_touching_protected_file_is_untouched(monkeypatch, tmp_path):
    """普通 bash 不该被兜底误伤 —— 结果原样返回。"""
    from src.agent import graph_thinking, tools

    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "keep.py").write_text("original", encoding="utf-8")
    (work / "tmp").mkdir()

    monkeypatch.setattr(tools, "WORKDIR", work)
    result = graph_thinking.run_single_tool("bash", {"command": "echo hello"})

    assert result.strip() == "hello"


def test_snapshot_is_cleaned_up_after_bash(monkeypatch, tmp_path):
    """比对完就把快照删掉 —— 否则每次 bash 都在 tmp/_protect/ 里留一份副本。"""
    from src.agent import graph_thinking, tools

    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "keep.py").write_text("original", encoding="utf-8")
    (work / "tmp").mkdir()

    monkeypatch.setattr(tools, "WORKDIR", work)
    graph_thinking.run_single_tool("bash", {"command": "echo hello"})

    protect_dir = work / "tmp" / "_protect"
    leftovers = list(protect_dir.iterdir()) if protect_dir.exists() else []
    assert leftovers == []


def test_snapshot_is_cleaned_up_after_bash_that_tripped_the_guard(monkeypatch, tmp_path):
    """触发兜底的那次调用也要清理 —— 恰恰是它最容易留下快照。"""
    from src.agent import graph_thinking, tools

    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "keep.py").write_text("original", encoding="utf-8")
    (work / "tmp").mkdir()

    monkeypatch.setattr(tools, "WORKDIR", work)
    graph_thinking.run_single_tool("bash", {"command": "echo tampered > src/keep.py"})

    protect_dir = work / "tmp" / "_protect"
    leftovers = list(protect_dir.iterdir()) if protect_dir.exists() else []
    assert leftovers == []


def test_same_size_change_is_detected(workdir):
    """内容变了但长度没变 —— 也得发现（stat 比对靠 mtime，不靠长度）。"""
    from src.agent.authz import protect

    (workdir / "src").mkdir(parents=True, exist_ok=True)
    (workdir / "tmp").mkdir(parents=True, exist_ok=True)
    target = workdir / "src" / "a.py"
    target.write_text("AAAA", encoding="utf-8")

    snap = protect.snapshot(workdir)
    target.write_text("BBBB", encoding="utf-8")

    report = protect.diff_and_restore(snap, workdir)
    protect.cleanup(snap)

    assert report["changed"] == ["src/a.py"]
    assert target.read_text(encoding="utf-8") == "AAAA"


def test_touched_but_unchanged_file_is_not_reported(workdir):
    """只改 mtime 不改内容 —— 不该报成「被修改」（否则是假警报）。"""
    from src.agent.authz import protect

    (workdir / "src").mkdir(parents=True, exist_ok=True)
    (workdir / "tmp").mkdir(parents=True, exist_ok=True)
    target = workdir / "src" / "a.py"
    target.write_text("AAAA", encoding="utf-8")

    snap = protect.snapshot(workdir)
    os.utime(target, (0, 0))

    report = protect.diff_and_restore(snap, workdir)
    protect.cleanup(snap)

    assert report["changed"] == []
    assert report["restored"] is False


def test_change_with_forged_mtime_is_a_known_gap(workdir):
    """已知边界：改了内容又把 mtime 改回去，stat 比对看不出来。

    这条测试把边界钉住 —— 将来有人看到「stat 比对够快」就把它当逐字节比对用，
    这条会拦住他。
    """
    from src.agent.authz import protect

    (workdir / "src").mkdir(parents=True, exist_ok=True)
    (workdir / "tmp").mkdir(parents=True, exist_ok=True)
    target = workdir / "src" / "a.py"
    target.write_text("AAAA", encoding="utf-8")

    snap = protect.snapshot(workdir)
    stat = target.stat()
    target.write_text("BBBB", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    report = protect.diff_and_restore(snap, workdir)
    protect.cleanup(snap)

    assert report["changed"] == []
    assert target.read_text(encoding="utf-8") == "BBBB"
