"""pending cache 里存的是「原始对话」，不是带标签的提示词。

标签是给模型看的结构边界；memory sub-agent 读到带标签的文本后，
可能把 <UserMessage>…</UserMessage> 当成记忆内容写进长期记忆。
"""


def test_pending_cache_stores_unlabeled_content(tmp_path, monkeypatch):
    from src.agent import graph_thinking
    from src.agent.memory.pending import PendingCache

    monkeypatch.setattr(graph_thinking, "_pending_cache",
                        PendingCache(str(tmp_path / "pending.db")))
    graph_thinking._save_to_pending(
        [{"role": "user", "content": "<UserMessage>帮我备份实验数据</UserMessage>"}],
        session_id="s1",
    )
    rows = graph_thinking._pending_cache.get_pending_by_session("s1")
    assert rows[0]["content"] == "帮我备份实验数据"


def test_pending_cache_strips_current_request_label(tmp_path, monkeypatch):
    from src.agent import graph_thinking
    from src.agent.memory.pending import PendingCache

    monkeypatch.setattr(graph_thinking, "_pending_cache",
                        PendingCache(str(tmp_path / "pending.db")))
    graph_thinking._save_to_pending(
        [{"role": "user", "content": "<CurrentRequest>做这个</CurrentRequest>"}],
        session_id="s1",
    )
    rows = graph_thinking._pending_cache.get_pending_by_session("s1")
    assert "<CurrentRequest>" not in rows[0]["content"]
    assert rows[0]["content"] == "做这个"
