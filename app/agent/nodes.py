from app.agent.state import AgentState
from app.integrations.deepseek import EmailAnalyzer
from app.integrations.gmail import GmailGateway


async def fetch_email_node(state: AgentState, gmail: GmailGateway) -> AgentState:
    fetched = await gmail.fetch_email(state["gmail_message_id"])
    state["metadata"] = {
        "fetched_email": fetched,
        "gmail_thread_id": fetched.gmail_thread_id,
    }
    return state


async def analyze_email_node(state: AgentState, analyzer: EmailAnalyzer) -> AgentState:
    email = state["email"]
    state["decision"] = await analyzer.analyze(email)
    return state
