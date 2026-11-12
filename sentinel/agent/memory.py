# Agent Memory - Short-term context + Optional RAG (Stretch)

class AgentMemory:
    """Manages agent memory: short-term conversation context and optional RAG."""
    
    def __init__(self):
        self.conversation_history = []
        # TODO: Add chromadb for RAG (stretch goal)
    
    def add_interaction(self, user_input: str, agent_response: str, tool_calls: list = None):
        """Store an interaction in memory."""
        self.conversation_history.append({
            "user": user_input,
            "agent": agent_response,
            "tool_calls": tool_calls or []
        })
    
    def get_recent_context(self, n: int = 5) -> list:
        """Get last n interactions for context."""
        return self.conversation_history[-n:]
    
    def clear(self):
        """Clear memory."""
        self.conversation_history = []