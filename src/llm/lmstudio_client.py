# LM Studio Client - OpenAI-compatible wrapper for local LLM

from openai import OpenAI
import os
import logging

logger = logging.getLogger(__name__)


class LMStudioClient:
    """Client for LM Studio's OpenAI-compatible API."""
    
    def __init__(self):
        base = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
        model = os.getenv("LMSTUDIO_MODEL", "qwen/qwen3.5-9b")
        self.client = OpenAI(base_url=base, api_key="lm-studio", timeout=2.0, max_retries=0)
        self.model = model
        self._available = None
    
    def is_available(self) -> bool:
        """Check if LM Studio server is reachable."""
        if self._available is not None:
            return self._available
        try:
            # Short timeout for quick fallback
            self.client.models.list()
            self._available = True
            logger.info(f"LM Studio connected: {self.model}")
        except Exception as e:
            self._available = False
            logger.warning(f"LM Studio not available: {e}")
        return self._available
    
    def chat_completion(self, messages, tools=None, tool_choice="auto", **kwargs):
        """Create a chat completion with optional tool calling."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=0.1,
            **kwargs
        )
        
        # Handle Qwen3 reasoning models that output to reasoning_content instead of content
        # If content is empty but reasoning_content has text, copy it over
        for choice in response.choices:
            msg = choice.message
            if (not msg.content or msg.content == "") and hasattr(msg, 'reasoning_content') and msg.reasoning_content:
                # For reasoning models, use reasoning_content as the actual response
                # We can't modify the message directly, but we can log it
                logger.debug(f"Model used reasoning_content: {msg.reasoning_content[:100]}...")
        
        return response