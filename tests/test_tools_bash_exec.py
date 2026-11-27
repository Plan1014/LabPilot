"""bash 工具的**执行层**测试 —— 命令跑起来之后会不会把线程挂死。

跟鉴权闸门无关：闸门早就放行了（审计日志里那条 L2 allow 就是证据），
是工具去执行、执行本身把线程挂住了。

两个症状：

1. 命令等 stdin 上的输入 —— agent 没人能回答，命令该立刻拿到 EOF 自己退出。
2. 命令超时 —— 必须**返回**。孙进程攥着 stdout 管道的时候，只杀直接子进程
   不够，`communicate()` 会一直等 EOF。
"""

import os
import threading
from contextlib import contextmanager

import pytest

from src.agent import paths, tools


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    monkeypatch.setattr(tools, "WORKDIR", tmp_path)
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _call_within(fn, seconds):
    """在后台线程里调 fn，最多等 seconds 秒；超时返回 (None, None)。

    测试要能**失败**，不能跟着一起挂死。
    """
    box = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("out"), box.get("error")


@contextmanager
def _blocking_stdin():
    """把本进程的 fd 0 换成一条**没有数据也不关闭**的管道。

    子进程继承的就是这条管道 → 它读 stdin 会一直等下去。

    为什么非得这么绕：pytest 默认会把 fd 0 重定向到 NUL，子进程立刻拿到
    EOF —— 那样测的是「拿到 EOF 时会不会挂」，不是「等输入时会不会挂」。
    """
    read_fd, write_fd = os.pipe()
    saved = os.dup(0)
    os.dup2(read_fd, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(read_fd)
        os.close(write_fd)
        os.close(saved)


def test_bash_does_not_hang_on_a_command_that_waits_for_stdin(workdir):
    """交互式脚本不该把工具线程挂死。

    agent 没法回答 stdin 上的提问 —— 命令应该立刻拿到 EOF 自己退出，
    而不是等到超时（更糟的是 Windows 上永远等不到）。
    """
    with _blocking_stdin():
        out, error = _call_within(
            lambda: tools.bash.invoke({"command": 'python -c "input()"'}), 20
        )
    assert error is None
    assert out is not None, "bash 还在等 stdin —— 工具线程被挂住了"


def test_bash_returns_when_the_command_exceeds_the_timeout(workdir, monkeypatch):
    """超时的命令必须**返回**，并且说清楚是超时。

    孙进程攥着 stdout 管道不放的时候，只杀直接子进程是不够的 ——
    杀完再 `communicate()` 还在等 EOF，线程就永久挂住了。
    """
    monkeypatch.setattr(tools, "BASH_TIMEOUT_S", 3, raising=False)
    cmd = (
        'python -c "import subprocess, sys, time; '
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        'time.sleep(30)"'
    )
    out, error = _call_within(
        lambda: tools.bash.invoke({"command": cmd}), 25
    )
    assert error is None
    assert out is not None, "超时之后 bash 没有返回 —— 进程树没被杀掉"
    assert "超时" in out


def test_bash_reports_the_stderr_of_a_failed_command(workdir):
    """命令失败时 stderr 要看得见。

    交互式脚本在 stdin 拿到 EOF 后会抛 EOFError —— 只回 stdout 的话，
    agent 只看到半句提示，看不出是失败还是成功。
    """
    out, error = _call_within(
        lambda: tools.bash.invoke(
            {"command": 'python -c "raise SystemExit(\'boom\')"'}
        ),
        20,
    )
    assert error is None
    assert out is not None
    assert "boom" in out
