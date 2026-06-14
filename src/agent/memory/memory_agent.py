"""记忆子Agent - 处理silent summary的独立Agent

使用LangGraph的create_react_agent构造，不手动处理tool解析
"""

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

WORD_LIMIT = 100

# SYSTEM_PROMPT = """你的任务是总结一段人工智能角色与人类之间的对话历史。你所给定的对话来自一个固定的时间窗口, 可能并不完整。由人工智能发送的消息以“assistant”角色标记。人工智能“assistant”还可以调用工具，工具的输出会在“tool”角色的消息中显示。人工智能在消息内容中所说的话被视为内心独白，用户看不到。用户唯一能看到的人工智能消息是使用“send_message”时发出的。用户发送的消息以“user”角色标记。“user”角色也用于重要系统事件，例如登录事件和心跳事件(心跳在用户未采取行动的情况下运行人工智能程序，使人工智能能够在用户未发送消息时采取行动)。从人工智能的视角(使用第一人称)总结对话中发生的事情。保持总结少于{WORD_LIMIT}个单词，不要超过这个字数限制。
# 请你以第一人称如实记录事件。详尽且简洁 —— 目标是保留足够上下文，使近期消息可理解、关键信息不丢失，避免重复工作或重犯错误。

# 记忆系统包括：
# 1. Core Memory (热记忆): 使用 save_core_block, append_core_block 写入
# 2. Archival Memory (长期记忆): 使用 remember_fact 写入重要事实

# 关键原则：
# - 热记忆写入有必要并需要实时回顾的记忆, 特别是模型所犯的错误和使用指南
# - 长期记忆写入需要保存的知识点
# - 识别对话中的关键进展、参数、错误和解决方案
# - 判断是否需要写入Core Memory (当前状态) 还是Archival Memory (持久事实)
# - Core Memory使用标签如: task, conclusion/parameter, conclusion/technique, error
# - Archival Memory使用remember_fact保存重要发现

# 输出格式：
# 当你完成记忆整理后，输出一行总结，说明你写了哪些记忆。
# """

SYSTEM_PROMPT = """
## 角色与任务
你是一个记忆管理助手，负责从对话历史中提取关键信息并分类存储。对话来自固定时间窗口，可能不完整。
请你以第一人称如实记录事件。详尽且简洁 —— 目标是保留足够上下文，使近期消息可理解、关键信息不丢失，避免重复工作或重犯错误。

## 消息角色说明
- `assistant`: AI的输出
- `tool`: 工具调用结果
- `thinking` AI的内心独白
- `text`: AI实际发送给用户的内容(用户可见)
- `user`: 用户消息或系统事件(登录/心跳)

## 记忆系统架构

### Core Memory(热记忆/缓存层):使用 save_core_block, append_core_block 写入
**特性**：高频访问、短期有效、实时回顾
**写入时机**:
- 当前任务状态/进度(task)
- 刚发现的错误及修复方案(error)
- 临时参数/中间结论(conclusion)
- 用户即时偏好或约束(persona)

**注意**: 不要写入重复的内容, 不要重复放入不同的标签, 写入core memory不要覆盖原来的error和conclusion

**标签规范**: `task`, `error`, `persona`, `conclusion`

### Archival Memory(长期记忆/持久层): 使用 remember_fact 写入重要事实
**特性**：低频访问、长期有效、知识沉淀
**写入时机**: 
- 用户的核心身份信息(姓名/职业/技术栈)
- 项目目标、研究方向等稳定事实
- 已验证的技术方案/最佳实践
- 用户明确表达的长期偏好
- 可复用的领域知识或经验总结

## 决策流程(关键！)
面对一条信息时，依次问自己：
1. 【时效性】这条信息下次对话还需要吗？
   - 否 → 不存储
   - 是 → 继续
2. 【使用频率】未来24小时内会频繁引用吗 ?
   - 是 → Core Memory + 对应标签
   - 否 → 继续
3. 【通用性】这条信息是特定任务的临时状态，还是可复用的知识？
   - 临时状态 → Core Memory
   - 可复用知识 → Archival Memory

## 输出要求
1. 先执行记忆写入操作(调用相应工具)
2. 最后输出一行总结，格式：
   `记忆更新: Core[{标签1},{标签2}] | Archival[{事实摘要}]`
3. 整体回复控制在 {WORD_LIMIT} 词以内，简洁优先

## 避免的陷阱
 不要把每条对话都存档(避免记忆污染)
 不要把临时参数写入长期记忆(避免过期信息)
 不要重复存储已存在的事实(先检索再写入)
 优先存储"为什么"和"怎么做"，而非"做了什么"
 错误信息必须带修复方案一起存储
 用户显式强调的内容优先升级存储层级
"""


# 预构建的memory agent(延迟初始化)
_memory_agent = None
_agent_lock = None


def _get_memory_agent():
    """Lazy init of memory react agent"""
    global _memory_agent, _agent_lock
    if _agent_lock is None:
        import threading
        _agent_lock = threading.Lock()

    with _agent_lock:
        if _memory_agent is None:
            # Lazy imports to avoid circular dependency
            from langgraph.prebuilt import create_react_agent
            from src.agent.tools import TOOLS
            from src.agent.llm import llm

            _memory_agent = create_react_agent(model=llm, tools=TOOLS)
        return _memory_agent


def memory_agent_summarize(messages: list, debug: bool = True) -> str:
    """独立记忆子agent，处理silent summary

    Args:
        messages: 待总结的对话消息列表
        debug: 是否输出调试信息

    Returns:
        总结结果的描述字符串
    """
    # 构建summary prompt
    summary_prompt = _build_summary_prompt(messages)

    # 获取prebuilt react agent
    agent = _get_memory_agent()

    # 调用agent
    if debug:
        print(f"\033[94m[Memory Agent]\033[0m Invoking with {len(messages)} messages")

    try:
        result = agent.invoke({
            "messages": [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=summary_prompt)
            ]
        })
    except Exception as e:
        return f"Error: {e}"

    # 获取最终响应
    final_text = ""
    if hasattr(result, "messages"):
        for msg in reversed(result.messages):
            if hasattr(msg, "content") and msg.content:
                if isinstance(msg.content, str):
                    final_text = msg.content
                    break
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            final_text = block.get("text", "")
                            break
                    if final_text:
                        break

    if debug:
        print(f"\033[94m[Memory Agent]\033[0m Final: {final_text[:200] if final_text else '(empty)'}")

    return final_text


def _build_summary_prompt(messages: list) -> str:
    """将消息列表格式化为文本"""
    lines = ["请整理以下对话内容的记忆, 注意core_memory是热点记忆：\n"]

    for i, msg in enumerate(messages):
        # 支持 dict 格式和 LangChain Message 对象
        if hasattr(msg, "get"):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "")

        if not isinstance(content, str):
            content = str(content)

        lines.append(f"=== {role.upper()} ===")
        lines.append(content)
        lines.append("")

    lines.append("\n请分析对话，提取关键信息并写入适当的记忆中。")
    return "\n".join(lines)


def process_pending_cache() -> Optional[str]:
    """处理pending cache中的内容

    Returns:
        总结结果字符串，如果无pending或不足3条消息返回None
    """
    from src.agent.memory.pending import PendingCache

    cache = PendingCache()
    pending = cache.get_pending()

    if not pending or len(pending) < 3:
        cache.close()
        return None

    result = memory_agent_summarize(pending)
    cache.clear_pending()
    cache.close()

    return result