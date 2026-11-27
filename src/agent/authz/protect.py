"""隔离兜底 —— 快照 / 比对 / 还原（spec §5.6）。

L1 黑名单是确定性的，但它挡不住所有 bash 形态（`python -c` 里拼出来的路径
提取不到）。这层是**执行后**的保险，不是主防线：

- 执行前：把受保护区域复制到 `tmp/_protect/<随机名>/`
- 执行后：比对快照与现状 → 被改/被删的还原，新出现的移出（**不销毁**）
- 有任何差异 → 本次调用判为失败，附还原报告交回主 agent

**不销毁用户的文件**：受保护区里新出现（或被改动）的文件，可能是用户自己
在编辑器里写的，不是 agent 写的。直接删掉就是毁掉别人的劳动成果。所以
「多出来的文件」和「被改动文件的当前版本」都**搬到隔离目录**
`.labpilot/protect-quarantine/<随机名>/`，报告里给出路径，人要的话自己取回。
还原（把快照版本写回原位）照旧做 —— 保护区还是要回到调用前的样子。

**为什么不用 git 还原**：`git checkout` 会把用户自己的未提交改动一起抹掉 ——
LabPilot 正在被开发，工作区随时有 WIP。快照存的是「执行那一刻」的真实状态。

**已知边界**（不要假装没有）：
- 快照目录在 `tmp/` 下，agent 理论上够得着（用随机名降低概率）。
- 挡不住「先找到快照再改源码」这种针对性行为。
- `bash(background=True)` 会打穿这层（Popen 立刻返回，还原跑完后台进程还没动
  文件）—— 这类 bash 由 L2 判官强制挡，不走快照路径。
- 还原会覆盖用户在快照窗口内的并发编辑。
- **`.git/` 不在受保护集合里**（spec §5.6 就是这么定的 —— git 每跑一次
  `status` 都会写 `.git/index`，纳入快照会变成天天误报）。`.git` 的直接写入
  由 L1 挡住（`paths` 把 `.git` 判成只读区）；但「脚本内部改 `.git`」这条路，
  这层兜底管不了。
- 比对先走 stat 再逐字节（见 `_differs`）—— 伪造 mtime 能躲过去，那属于
  针对性行为，不在这层的职责内。
"""

import fnmatch
import shutil
import uuid
from pathlib import Path

from src.agent import paths

# 生成物：跑一次脚本就会冒出来的东西，不是「源码被改」。
# 比对时必须忽略 —— 否则 agent 每跑一次项目脚本就被判「bash 失败」，
# 属于典型的假警报。
GENERATED_ARTIFACT_PATTERNS: tuple[str, ...] = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "*.egg-info",
    "node_modules",
    ".DS_Store",
    "*.log",
    ".coverage",
)

# 快照根目录（相对 workdir）
PROTECT_DIR_NAME = "_protect"

# 隔离目录（相对 workdir）—— 被移出来的文件放这儿，等人来取。
# 放在 `.labpilot/` 下（不是 `tmp/`）：那里 agent 写不了，
# 免得「隔离」的东西被 agent 自己顺手改掉。
QUARANTINE_DIR_NAME = "protect-quarantine"
QUARANTINE_PARENT = ".labpilot"


def is_generated_artifact(path: Path, workdir: Path) -> bool:
    """这个路径是不是生成物。

    逐段比对相对路径 —— 只要**任何一段**命中生成物模式就算数
    （`src/__pycache__/x.pyc` 的命中点在第二段）。
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(Path(workdir).resolve())
    except (ValueError, OSError):
        rel = p
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in rel.parts
        for pattern in GENERATED_ARTIFACT_PATTERNS
    )


def _ignore_generated(_dirpath, names):
    """copytree 的 ignore 回调 —— 生成物不拷进快照。"""
    ignored = set()
    for name in names:
        for pattern in GENERATED_ARTIFACT_PATTERNS:
            if fnmatch.fnmatch(name, pattern):
                ignored.add(name)
                break
    return ignored


def snapshot(workdir: Path) -> Path:
    """把受保护区域复制到 `tmp/_protect/<随机名>/`，返回快照目录。

    受保护区域 = `paths.PROTECTED_DIRS` 里的目录（`frontend` 只取 `src/`）
    + `paths.PROTECTED_ROOT_FILES` 里的根文件。
    """
    workdir = Path(workdir)
    snap_root = workdir / "tmp" / PROTECT_DIR_NAME / uuid.uuid4().hex
    snap_root.mkdir(parents=True, exist_ok=True)

    for rel in paths.PROTECTED_DIRS:
        src = workdir / rel
        if not src.is_dir():
            continue
        dst = snap_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src, dst, dirs_exist_ok=True, ignore=_ignore_generated
        )

    for name in paths.PROTECTED_ROOT_FILES:
        src = workdir / name
        if src.is_file():
            shutil.copy2(src, snap_root / name)

    return snap_root


def _collect(root: Path, rel_prefix: str, workdir: Path) -> dict[str, Path]:
    """列出目录下的文件 → {workdir 相对路径: 绝对路径}，跳过生成物。"""
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if is_generated_artifact(path, workdir):
            continue
        key = f"{rel_prefix}/{path.relative_to(root).as_posix()}"
        out[key] = path
    return out


def _differs(a: Path, b: Path) -> bool:
    """两个文件内容是否不同。

    先比 stat（size + mtime_ns），一致就直接判「没变」。逐字节读 100 个文件
    要 1.4 秒，stat 只要 0.2 秒 —— 而这笔开销 bash 每跑一次都要摊。
    `copy2` 保留 mtime，所以没被动过的文件两边 mtime 完全相同。

    stat 不一致才真的读字节确认：`touch` 只改 mtime、内容没变，
    不该报成「被修改」。

    **已知边界**：改了内容又把 mtime 改回去，stat 比对看不出来。
    这是故意接受的 —— 伪造 mtime 本身就是针对性的破坏，不在这一层的职责内
    （真要防这个，得逐字节比对，代价是每次 bash 多花 1.2 秒）。
    """
    try:
        sa, sb = a.stat(), b.stat()
        if sa.st_size != sb.st_size:
            return True
        if sa.st_mtime_ns == sb.st_mtime_ns:
            return False
        return a.read_bytes() != b.read_bytes()
    except OSError:
        return True


def _copy_back(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _quarantine_dir(workdir: Path) -> Path:
    """这次隔离用的目录（还不建，用到才建）。"""
    return Path(workdir) / QUARANTINE_PARENT / QUARANTINE_DIR_NAME / uuid.uuid4().hex


def _preserve(live_path: Path, key: str, quarantine_root: Path) -> bool:
    """把现场那份内容**抄一份**到隔离目录 —— 原文件不动。

    抄成功 → True（内容留住了）；抄失败 → False（调用方决定怎么办）。
    """
    dest = quarantine_root / key
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live_path, dest)
        return True
    except OSError:
        return False


def _remove_to_quarantine(live_path: Path, key: str, quarantine_root: Path) -> bool:
    """把现场那个文件**搬走**到隔离目录 —— 受保护区里不留它，但也不销毁。

    搬成功 → True；搬失败 → False（文件留在原地，调用方报告出来）。
    """
    dest = quarantine_root / key
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(live_path), str(dest))
        return True
    except OSError:
        return False


def _prune_empty_dirs(path: Path, stop: Path) -> None:
    """删掉空目录（往上删到 stop 为止）。删不掉就算了。"""
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def diff_and_restore(snapshot_dir: Path, workdir: Path) -> dict:
    """比对快照与现状，把差异还原掉。

    返回
    `{"changed": [...], "removed": [...], "added": [...], "quarantined": [...],
    "quarantine_dir": str | None, "restored": bool}`，
    路径是相对 workdir 的正斜杠形式（`src/agent/tools.py`）。

    - changed：内容被改 → 从快照还原；现场那份**搬进隔离目录**（不销毁）
    - removed：文件被删 → 从快照还原
    - added：新出现的文件 → **搬进隔离目录**（不销毁）

    隔离目录在 `.labpilot/protect-quarantine/<随机名>/`，报告里给出路径。
    """
    workdir = Path(workdir)
    snapshot_dir = Path(snapshot_dir)

    changed: list[str] = []
    removed: list[str] = []
    added: list[str] = []
    quarantined: list[str] = []
    quarantine_failed: list[str] = []
    restored = False

    quarantine_root: Path | None = None

    def _root() -> Path:
        nonlocal quarantine_root
        if quarantine_root is None:
            quarantine_root = _quarantine_dir(workdir)
        return quarantine_root

    def preserve(live_path: Path, key: str) -> bool:
        """把现场那份内容抄一份进隔离目录（原文件不动）。"""
        if _preserve(live_path, key, _root()):
            quarantined.append(key)
            return True
        quarantine_failed.append(key)
        return False

    def remove_to_quarantine(live_path: Path, key: str) -> bool:
        """把现场那个文件搬进隔离目录（受保护区里不留它，也不销毁）。"""
        if _remove_to_quarantine(live_path, key, _root()):
            quarantined.append(key)
            return True
        quarantine_failed.append(key)
        return False

    for rel in paths.PROTECTED_DIRS:
        snap_files = _collect(snapshot_dir / rel, rel, workdir)
        live_files = _collect(workdir / rel, rel, workdir)
        live_root = workdir / rel

        for key, snap_path in snap_files.items():
            live_path = live_files.get(key)
            if live_path is None:
                removed.append(key)
                _copy_back(snap_path, workdir / key)
                restored = True
            elif _differs(snap_path, live_path):
                changed.append(key)
                # 现场那份先留一份底 —— 可能是用户自己改的。
                # 留底失败也照样还原（保护区优先），但报告里会说明。
                preserve(live_path, key)
                _copy_back(snap_path, workdir / key)
                restored = True

        for key, live_path in live_files.items():
            if key in snap_files:
                continue
            added.append(key)
            # 新出现的文件不删 —— 搬进隔离目录，人要的话自己取回。
            # 搬不动就留在原地（不销毁），报告里会说出来。
            remove_to_quarantine(live_path, key)
            _prune_empty_dirs(live_path, live_root)
            restored = True

    for name in paths.PROTECTED_ROOT_FILES:
        snap_path = snapshot_dir / name
        live_path = workdir / name
        if snap_path.is_file():
            if not live_path.is_file():
                removed.append(name)
                _copy_back(snap_path, live_path)
                restored = True
            elif _differs(snap_path, live_path):
                changed.append(name)
                preserve(live_path, name)
                _copy_back(snap_path, live_path)
                restored = True
        elif live_path.is_file():
            added.append(name)
            remove_to_quarantine(live_path, name)
            restored = True

    return {
        "changed": sorted(changed),
        "removed": sorted(removed),
        "added": sorted(added),
        "quarantined": sorted(quarantined),
        "quarantine_failed": sorted(quarantine_failed),
        "quarantine_dir": str(quarantine_root) if quarantine_root else None,
        "restored": restored,
    }


def format_report(report: dict) -> str:
    """把比对结果渲染成给主 agent 看的清单。"""
    lines = []
    for label, key in (("被修改", "changed"), ("被删除", "removed"), ("被新增", "added")):
        for path in report.get(key, []):
            lines.append(f"- {label}：{path}")
    quarantined = report.get("quarantined") or []
    if quarantined:
        lines.append("")
        lines.append("以下文件已移到隔离目录（没有删除，需要的话自己取回）：")
        for path in quarantined:
            lines.append(f"- {path}")
        lines.append(f"隔离目录：{report.get('quarantine_dir')}")
    return "\n".join(lines)


def cleanup(snapshot_dir: Path) -> None:
    """删掉快照目录 —— 比对完就没用了。

    不删的话每次 bash 都在 tmp/_protect/ 里留一份受保护区的副本，
    跑几十次就是几百 MB。
    """
    shutil.rmtree(snapshot_dir, ignore_errors=True)
