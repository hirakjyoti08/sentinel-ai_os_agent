# NVIDIA Nemotron Client - OpenAI-compatible wrapper for NVIDIA NIM API

from openai import OpenAI
import os
import logging

logger = logging.getLogger(__name__)


class NemotronClient:
    """Client for NVIDIA Nemotron via the OpenAI-compatible NIM API."""
    
    def __init__(self):
        base = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
        api_key = os.getenv("NVIDIA_API_KEY", "")
        model = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
        self.model = model
        self._available = None
        self._configured = bool(api_key and api_key not in ("", "PASTE_YOUR_NVIDIA_KEY_HERE"))
        if self._configured:
            self.client = OpenAI(base_url=base, api_key=api_key, timeout=30.0, max_retries=1)
        else:
            self.client = None
    
    @property
    def configured(self) -> bool:
        """Check if NVIDIA API key is configured."""
        return self._configured

    def is_available(self) -> bool:
        """Check if NVIDIA Nemotron API is reachable."""
        if not self._configured:
            self._available = False
            logger.info("NVIDIA Nemotron not configured: missing NVIDIA_API_KEY")
            return False
        if self._available is not None:
            return self._available
        try:
            self.client.models.list()
            self._available = True
            logger.info(f"NVIDIA Nemotron connected: {self.model}")
        except Exception as e:
            self._available = False
            logger.warning(f"NVIDIA Nemotron not available: {e}")
        return self._available
    
    def chat_completion(self, messages, tools=None, tool_choice="auto", stream=False, **kwargs):
        """Create a chat completion with optional tool calling and streaming."""
        params = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": stream,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice
        params.update(kwargs)

        response = self.client.chat.completions.create(**params)
        return response
