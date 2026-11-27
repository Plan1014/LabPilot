"""跨测试文件共享的夹具。

目前只有一个：链接形态的工作区（符号链接 / Windows junction）。
"""

import os
import subprocess

import pytest


@pytest.fixture
def junction_workdir(tmp_path):
    """一个「链接形态」的工作区：链接 -> 真实目录。

    checkout 有可能是个链接（符号链接或 Windows 的 junction）——
    WORKDIR 指向链接，路径解析后指向真实目录。这种形态下路径比较
    必须仍然认得出「在工作区内」，否则所有路径都被判成 outside，
    写文件全被 L1 拒（假阳性）。

    造不出来就跳过 —— 链接权限因机器而异，不该让整个套件红。
    """
    real = tmp_path / "real"
    (real / "tmp").mkdir(parents=True)
    (real / "src").mkdir(parents=True)

    link = tmp_path / "link"
    created = False
    try:
        link.symlink_to(real, target_is_directory=True)
        created = True
    except (OSError, NotImplementedError):
        # Windows 上 os.symlink 要特权（WinError 1314），退回 junction ——
        # mklink /J 不需要特权，而且 Python 的 resolve() 认得出它。
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(real)],
                capture_output=True,
                check=False,
            )
            created = result.returncode == 0
    if not created:
        pytest.skip("本机造不出目录链接（symlink 与 mklink 都失败）")

    yield link

    # 只删链接本身 —— 别顺着链接把目标目录删了。
    try:
        os.rmdir(link)
    except OSError:
        pass
