import subprocess
import sys

CODE = (
    "import sys, src.agent.config;"
    "assert 'src.agent.graph' not in sys.modules, "
    "sorted(m for m in sys.modules if m.startswith('src.agent'))"
)


def test_importing_a_submodule_does_not_load_the_graph():
    """导入 src.agent.config 不应连带加载 graph（那会拉起 47 秒的模型/向量库链）。"""
    subprocess.run([sys.executable, "-c", CODE], check=True)
