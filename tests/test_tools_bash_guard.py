"""bash 工具的命令级判定测试。

这些函数住在 `src/agent/tools.py`（spec D13：bash 的额外判定不做在闸门层，
要做就做在既有实现里）。两条铁律：

1. **不误伤** —— 只读命令（cat / grep / diff / python 跑脚本）永远放行。
   `read_file` 工具坏了的时候，agent 改用 bash 读源码是合法退路。
2. **只允许漏判，不允许错判** —— 解析不出的形态一律判「没命中」，放行。
"""

import pytest

from src.agent import paths, tools


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """把 paths 与 tools 的 WORKDIR 都指到临时目录。

    两处都要替换：paths.WORKDIR 决定区域判定，tools.WORKDIR 决定 subprocess 的 cwd。
    """
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tmp").mkdir()
    return tmp_path


# ==================== 反向用例：必须放行 ====================

def test_cat_source_file_is_not_a_write():
    assert tools.bash_write_targets("cat src/foo.py") == []


def test_grep_in_source_dir_is_not_a_write():
    assert tools.bash_write_targets("grep -rn x src/") == []


def test_diff_is_not_a_write():
    assert tools.bash_write_targets("diff src/a.py tmp/b.py") == []


def test_echo_mentioning_protected_path_is_not_a_write():
    # echo "会改 src/x.py" > tmp/log.txt  → 目标只有 tmp/log.txt
    assert tools.bash_write_targets('echo "会改 src/x.py" > tmp/log.txt') == ["tmp/log.txt"]


def test_python_running_project_script_is_not_a_write():
    assert tools.bash_write_targets("python src/x.py") == []


def test_read_only_bash_recognised():
    assert tools.is_read_only_bash("cat src/foo.py")


def test_read_only_bash_with_redirect_is_not_read_only():
    assert not tools.is_read_only_bash("echo hi > tmp/a.txt")


def test_read_only_bash_with_pipe_to_read_only_is_read_only():
    assert tools.is_read_only_bash("cat src/a.py | grep x")


def test_pipeline_with_write_segment_is_not_read_only():
    assert not tools.is_read_only_bash("cat src/a.py | tee tmp/a.txt")


def test_find_with_delete_is_not_read_only():
    assert not tools.is_read_only_bash("find tmp -delete")


# ==================== 正向用例：必须解析出写目标 ====================

def test_redirect_target():
    assert tools.bash_write_targets("echo hi > src/x.py") == ["src/x.py"]


def test_append_redirect_target():
    assert tools.bash_write_targets("echo hi >> src/x.py") == ["src/x.py"]


def test_redirect_without_space():
    assert tools.bash_write_targets("echo hi >src/x.py") == ["src/x.py"]


def test_fd_duplicate_is_not_a_file_target():
    assert tools.bash_write_targets("cat src/a.py 2>&1") == []


def test_tee_targets():
    assert tools.bash_write_targets("tee src/x.py") == ["src/x.py"]


def test_sed_inplace_target():
    assert tools.bash_write_targets("sed -i 's/a/b/' src/x.py") == ["src/x.py"]


def test_sed_without_inplace_is_not_a_write():
    assert tools.bash_write_targets("sed 's/a/b/' src/x.py") == []


def test_cp_destination_only():
    assert tools.bash_write_targets("cp src/a.py tmp/b.py") == ["tmp/b.py"]


def test_mv_source_and_destination():
    assert tools.bash_write_targets("mv src/a.py tmp/b.py") == ["src/a.py", "tmp/b.py"]


def test_rm_targets():
    assert tools.bash_write_targets("rm -rf src/x") == ["src/x"]


def test_dd_of_target():
    assert tools.bash_write_targets("dd if=/dev/zero of=src/x bs=1") == ["src/x"]


def test_mkdir_in_protected_dir_is_a_write():
    assert tools.bash_write_targets("mkdir src/newdir") == ["src/newdir"]
    assert tools.bash_write_targets("mkdir -p src/newdir") == ["src/newdir"]


def test_touch_in_protected_dir_is_a_write():
    assert tools.bash_write_targets("touch src/x.py") == ["src/x.py"]


def test_split_on_operators():
    assert tools.split_bash_segments("cat a && sed -i b c || echo d ; ls | grep x") == [
        "cat a",
        "sed -i b c",
        "echo d",
        "ls",
        "grep x",
    ]


def test_split_does_not_break_inside_quotes():
    assert tools.split_bash_segments("echo 'a && b'") == ["echo 'a && b'"]


# ==================== 区域判定 ====================

def test_write_to_src_is_blocked(workdir):
    reason = tools.bash_blocked_write_target("echo hi > src/x.py")
    assert reason is not None and "src/x.py" in reason


def test_write_to_tmp_is_not_blocked(workdir):
    assert tools.bash_blocked_write_target("echo hi > tmp/a.txt") is None


def test_write_outside_workdir_is_not_blocked(workdir):
    assert tools.bash_blocked_write_target("echo hi > ../outside.txt") is None


def test_compound_read_then_write_names_the_segment(workdir):
    reason = tools.bash_blocked_write_target("cat src/a.py && sed -i 's/a/b/' src/a.py")
    assert reason is not None
    assert "sed" in reason and "src/a.py" in reason


def test_read_only_command_is_never_blocked(workdir):
    assert tools.bash_blocked_write_target("cat src/a.py") is None
    assert tools.bash_blocked_write_target("grep -rn foo src/") is None


# ==================== bash() 工具本身 ====================

def test_bash_tool_refuses_before_execution(workdir):
    out = tools.bash.invoke({"command": "echo hi > src/x.py"})
    assert "危险行为拒绝执行" in out
    assert not (workdir / "src" / "x.py").exists()


def test_bash_tool_still_allows_reading_source(workdir):
    (workdir / "src" / "x.py").write_text("hello", encoding="utf-8")
    out = tools.bash.invoke({"command": "cat src/x.py"})
    assert "hello" in out
