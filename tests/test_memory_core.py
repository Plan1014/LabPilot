"""Test: CoreMemoryManager basic functionality."""

import sys
import tempfile
from pathlib import Path

# Isolated import: load constants first, then core module
# by patching the import chain to avoid triggering src.agent package init (requires langgraph)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Pre-load constants module to avoid circular import through src.agent.__init__
import importlib.util
constants_spec = importlib.util.spec_from_file_location(
    "src.agent.memory.constants",
    Path(__file__).parent.parent / "src" / "agent" / "memory" / "constants.py"
)
constants_module = importlib.util.module_from_spec(constants_spec)
sys.modules["src.agent.memory.constants"] = constants_module
constants_spec.loader.exec_module(constants_module)

# Pre-load config module (needed by core.py for WORKDIR)
config_spec = importlib.util.spec_from_file_location(
    "src.agent.config",
    Path(__file__).parent.parent / "src" / "agent" / "config.py"
)
config_module = importlib.util.module_from_spec(config_spec)
sys.modules["src.agent.config"] = config_module
config_spec.loader.exec_module(config_module)

# Now load core module
spec = importlib.util.spec_from_file_location(
    "src.agent.memory.core",
    Path(__file__).parent.parent / "src" / "agent" / "memory" / "core.py"
)
core_module = importlib.util.module_from_spec(spec)
sys.modules["src.agent.memory.core"] = core_module
spec.loader.exec_module(core_module)

Block = core_module.Block
BlockHistory = core_module.BlockHistory
CoreMemoryManager = core_module.CoreMemoryManager

def test_block_creation():
    """Test Block creation"""
    block = Block(
        id="test-1",
        label="task",
        value="PDH锁定校准中",
        description="当前任务状态",
        limit=4000,
        read_only=False,
        created_at=1234567890.0,
        updated_at=1234567890.0,
    )
    assert block.label == "task"
    assert block.value == "PDH锁定校准中"

def test_core_memory_manager_init():
    """Test CoreMemoryManager initialization with temp db"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blocks.db"
        manager = CoreMemoryManager(db_path=str(db_path))

        cursor = manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [r[0] for r in cursor.fetchall()]
        assert "blocks" in tables
        assert "block_history" in tables
        manager.close()

def test_save_and_get_block():
    """Test save and get block"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blocks.db"
        manager = CoreMemoryManager(db_path=str(db_path))

        manager.save_block("task", "PDH锁定校准中", description="当前任务状态")

        block = manager.get_block("task")
        assert block is not None
        assert block.label == "task"
        assert block.value == "PDH锁定校准中"
        manager.close()

def test_block_history():
    """Test BlockHistory is recorded on save"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blocks.db"
        manager = CoreMemoryManager(db_path=str(db_path))

        manager.save_block("task", "初始状态")

        manager.save_block("task", "新状态")

        history = manager.get_block_history("task")
        assert len(history) == 2
        manager.close()

def test_compile():
    """Test compile() renders correct XML"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blocks.db"
        manager = CoreMemoryManager(db_path=str(db_path))

        manager.save_block("task", "PDH锁定中", description="当前任务")
        manager.save_block("conclusion/parameter", "P=15, I=3", description="最优参数")

        xml = manager.compile()
        assert "<task>" in xml
        assert "<description>\n当前任务\n</description>" in xml
        assert "<value>\nPDH锁定中\n</value>" in xml
        # label with "/" is escaped to "_" in XML tags
        assert "<conclusion_parameter>" in xml
        manager.close()

if __name__ == "__main__":
    test_block_creation()
    test_core_memory_manager_init()
    test_save_and_get_block()
    test_block_history()
    test_compile()
    print("All tests passed!")