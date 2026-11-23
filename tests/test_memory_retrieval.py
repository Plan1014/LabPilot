import src.agent.memory.archival_store as store
from src.agent.prompt_context import RetrievedMemory


class _StubCollection:
    """假的 chroma collection：只实现 query() 的返回形状。"""

    def __init__(self, documents, distances, metadatas=None):
        self._documents = documents
        self._distances = distances
        self._metadatas = metadatas or [{} for _ in documents]
        self.calls = []

    def query(self, query_texts=None, n_results=None, include=None):
        self.calls.append({"n_results": n_results, "include": include})
        return {
            "documents": [self._documents[:n_results]],
            "distances": [self._distances[:n_results]],
            "metadatas": [self._metadatas[:n_results]],
        }


def _retriever(facts_doc, facts_dist, summaries_doc=None, summaries_dist=None):
    r = store.MemoryRetriever.__new__(store.MemoryRetriever)
    r.facts_col = _StubCollection(facts_doc, facts_dist)
    r.summaries_col = _StubCollection(
        summaries_doc or [], summaries_dist or []
    )
    r.sql_conn = None
    return r


def test_facts_above_threshold_are_dropped():
    r = _retriever(["噪声事实"], [0.62])
    result = r.retrieve("你有哪些工具可用？", threshold=0.45)
    assert result.facts == []


def test_facts_below_threshold_are_kept():
    r = _retriever(["相关事实"], [0.359])
    result = r.retrieve("帮我备份实验数据", threshold=0.45)
    assert [f.text for f in result.facts] == ["相关事实"]


def test_summaries_respect_threshold():
    r = _retriever([], [], ["一条旧摘要"], [0.7])
    result = r.retrieve("随便问问", threshold=0.45)
    assert result.summaries == []


def test_no_threshold_keeps_everything():
    """模型主动 search_memory 走这条：不过门槛。"""
    r = _retriever(["噪声事实"], [0.62])
    result = r.retrieve("随便问问", threshold=None)
    assert [f.text for f in result.facts] == ["噪声事实"]


def test_query_requests_distances():
    r = _retriever(["x"], [0.1])
    r.retrieve("q", threshold=0.45)
    assert "distances" in r.facts_col.calls[0]["include"]


def test_candidates_exceed_the_limit():
    r = _retriever(["a", "b", "c", "d", "e"], [0.1, 0.2, 0.3, 0.4, 0.5])
    result = r.retrieve("q", threshold=0.45)
    assert len(result.facts) == 3


def test_retrieve_context_has_no_task_events():
    """事件/日志不再注入提示词 —— task_states 查询整段移除。"""
    r = _retriever(["x"], [0.1])
    text = r.retrieve_context("q")
    assert "正在运行的后台任务" not in text
    assert "log_errors" not in text


def test_retrieve_context_returns_sentinel_when_empty():
    r = _retriever([], [])
    assert r.retrieve_context("q") == "当前无长期记忆记录。"


def test_retrieve_context_keeps_plain_text():
    r = _retriever(["相关事实"], [0.1])
    text = r.retrieve_context("q")
    assert "相关事实" in text
    assert "<RetrievedMemory>" not in text


def test_retrieve_returns_retrieved_memory_type():
    r = _retriever(["x"], [0.1])
    assert isinstance(r.retrieve("q", threshold=0.45), RetrievedMemory)
