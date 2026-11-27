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


# ==================== find / diff 的隐藏写目标 ====================
# `find` 和 `diff` 都在只读命令表里，但它们各自藏着一个能把结果写进文件的选项。
# 只读表说"只看不改"，这两个选项一出现就不成立了。

def test_find_fprint_target_is_a_write(workdir):
    assert tools.bash_write_targets("find src -fprint src/out.txt") == ["src/out.txt"]


def test_find_fprintf_target_is_a_write(workdir):
    assert tools.bash_write_targets("find src -fprintf src/out.txt") == ["src/out.txt"]


def test_find_without_write_option_is_not_a_write():
    assert tools.bash_write_targets("find src -name '*.py'") == []


def test_find_with_fprint_is_not_read_only():
    assert not tools.is_read_only_bash("find src -fprint src/out.txt")


def test_find_fprint_into_protected_dir_is_blocked(workdir):
    reason = tools.bash_blocked_write_target("find src -fprint src/out.txt")
    assert reason is not None and "src/out.txt" in reason


def test_diff_output_target_is_a_write(workdir):
    assert tools.bash_write_targets("diff a b --output=src/x.txt") == ["src/x.txt"]


def test_diff_with_output_is_not_read_only():
    assert not tools.is_read_only_bash("diff a b --output=tmp/x.txt")


# ==================== git 破坏性命令 ====================
# git 不改"文件路径"，它直接改工作区和历史 —— 效果比改文件更狠，
# 而且用户的未提交改动一旦没了，快照兜底也救不回来（.git 不在受保护集合里）。

def test_git_reset_hard_is_blocked(workdir):
    reason = tools.git_blocked_reason("git reset --hard")
    assert reason is not None and "reset" in reason


def test_git_reset_without_hard_is_not_blocked(workdir):
    assert tools.git_blocked_reason("git reset HEAD~1") is None


def test_git_checkout_dot_is_blocked(workdir):
    assert tools.git_blocked_reason("git checkout .") is not None


def test_git_checkout_path_is_blocked(workdir):
    (workdir / "src" / "x.py").write_text("hi", encoding="utf-8")
    assert tools.git_blocked_reason("git checkout src/x.py") is not None


def test_git_checkout_branch_is_not_blocked(workdir):
    # 切分支是正常操作，不能误伤（分支名可以带斜杠）
    assert tools.git_blocked_reason("git checkout feat/tool-authz") is None


def test_git_checkout_dash_dash_path_is_blocked(workdir):
    assert tools.git_blocked_reason("git checkout -- src/x.py") is not None


def test_git_restore_is_blocked(workdir):
    assert tools.git_blocked_reason("git restore src/x.py") is not None


def test_git_restore_staged_only_is_not_blocked(workdir):
    assert tools.git_blocked_reason("git restore --staged src/x.py") is None


def test_git_clean_is_blocked(workdir):
    assert tools.git_blocked_reason("git clean -fd") is not None


def test_git_clean_dry_run_is_not_blocked(workdir):
    assert tools.git_blocked_reason("git clean -n") is None


def test_git_push_is_blocked(workdir):
    assert tools.git_blocked_reason("git push origin main") is not None


def test_git_branch_force_delete_is_blocked(workdir):
    assert tools.git_blocked_reason("git branch -D feat/x") is not None


def test_git_stash_drop_is_blocked(workdir):
    assert tools.git_blocked_reason("git stash drop") is not None


def test_git_stash_is_not_blocked(workdir):
    assert tools.git_blocked_reason("git stash") is None


def test_git_status_is_not_blocked(workdir):
    assert tools.git_blocked_reason("git status") is None


def test_git_compound_command_names_the_segment(workdir):
    reason = tools.git_blocked_reason("git status && git reset --hard")
    assert reason is not None and "第 2 段" in reason


def test_bash_tool_refuses_git_reset_hard(workdir):
    out = tools.bash.invoke({"command": "git reset --hard"})
    assert "危险行为拒绝执行" in out


def test_authz_l1_blocks_git_reset_hard(workdir):
    from src.agent.authz.rules import find_denied_write_target

    reason = find_denied_write_target("bash", {"command": "git reset --hard"})
    assert reason is not None


def test_authz_l1_allows_git_status(workdir):
    from src.agent.authz.rules import find_denied_write_target

    assert find_denied_write_target("bash", {"command": "git status"}) is None


# ==================== git 只读子命令 ====================

def test_git_status_is_read_only_bash():
    assert tools.is_read_only_bash("git status")


def test_git_log_is_read_only_bash():
    assert tools.is_read_only_bash("git log --oneline")


def test_git_diff_is_read_only_bash():
    assert tools.is_read_only_bash("git diff")


def test_git_global_option_still_read_only():
    assert tools.is_read_only_bash("git --no-pager status")


def test_git_write_subcommand_is_not_read_only():
    assert not tools.is_read_only_bash("git reset --hard")


def test_git_diff_with_output_file_is_not_read_only():
    assert not tools.is_read_only_bash("git diff --output=tmp/d.txt")


# ==================== 模式/属主/大小不是路径（评审 C2）====================
# `chmod +x tmp/run.sh` 里第一个位置参数是模式，不是路径。把它当写目标，
# L1 会把 `+x` 说成「只读区域」并硬拒 —— agent 在沙箱里最常见的操作之一
# 被彻底堵死，而且卡在确定性拒绝层，判官和用户都放行不了。

def test_chmod_mode_literal_is_not_a_write_target():
    assert tools.bash_write_targets("chmod +x tmp/run.sh") == ["tmp/run.sh"]


def test_chmod_numeric_mode_is_not_a_write_target():
    assert tools.bash_write_targets("chmod 755 tmp/run.sh") == ["tmp/run.sh"]


def test_chown_owner_literal_is_not_a_write_target():
    assert tools.bash_write_targets("chown me tmp/x") == ["tmp/x"]


def test_truncate_size_literal_is_not_a_write_target():
    assert tools.bash_write_targets("truncate -s 100 tmp/x") == ["tmp/x"]


def test_chmod_on_protected_file_is_still_blocked(workdir):
    reason = tools.bash_blocked_write_target("chmod +x src/app.py")
    assert reason is not None
    assert "src/app.py" in reason


def test_chmod_plus_x_in_tmp_is_not_blocked(workdir):
    assert tools.bash_blocked_write_target("chmod +x tmp/run.sh") is None


def test_sed_without_file_operand_has_no_write_target():
    # `sed -i s/a/b/` 没有文件操作数 —— 改的是标准输入，不动文件
    assert tools.bash_write_targets("sed -i s/a/b/") == []
