from langgraph.graph import END, StateGraph

from app.agent.state import AgentState


def build_graph():
    graph = StateGraph(AgentState)

    async def start(state: AgentState) -> AgentState:
        return state

    async def finish(state: AgentState) -> AgentState:
        return state

    graph.add_node("start", start)
    graph.add_node("finish", finish)
    graph.set_entry_point("start")
    graph.add_edge("start", "finish")
    graph.add_edge("finish", END)
    return graph.compile()
