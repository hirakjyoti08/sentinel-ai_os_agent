# Agent Core - Plan/Act/Observe/Reflect Loop with LLM Integration

import json
import logging
import os
from typing import List, Dict, Any, Optional

import google.generativeai as genai
from groq import Groq
from dotenv import load_dotenv

from src.tools.registry import get_tool_schemas, get_gemini_tool_schemas, execute_tool, get_tool_tiers
from src.safety.permissions import get_tool_tier, confirm_action
from src.safety.circuit_breaker import CircuitBreaker
from src.storage.audit_log import log_action, update_result, get_last_action, mark_undone
from src.agent.prompts import SYSTEM_PROMPT, PLANNING_PROMPT, REFLECTION_PROMPT, TOOL_RESULT_SUMMARY_PROMPT
from src.agent.memory import AgentMemory
from src.llm.lmstudio_client import LMStudioClient

# Setup logging
logger = logging.getLogger(__name__)


def _render_ascii_bar(percent: float, width: int = 10) -> str:
    """Render a compact ASCII gauge bar."""
    filled = max(0, min(width, int(round((percent / 100.0) * width))))
    return f"[{'█' * filled}{'░' * (width - filled)}] {percent:>5.1f}%"


class Agent:
    """Main agent class for OS management with Plan-Act-Observe-Reflect loop."""
    
    def __init__(self):
        self.memory = AgentMemory()
        self.circuit_breaker = CircuitBreaker()
        self.llm_provider_status = {}
        
        # Initialize LLM clients
        self._init_llm_clients()
        
        # Tool schemas for function calling
        self.tool_schemas_openai = get_tool_schemas()  # OpenAI format for Groq
        self.tool_schemas_gemini = get_gemini_tool_schemas()  # Gemini format
        self.tool_tiers = get_tool_tiers()
    
    def _init_llm_clients(self):
        """Initialize LLM clients with priority: LM Studio -> Groq -> Gemini."""
        load_dotenv()

        # LM Studio (primary - local, unlimited)
        self.lmstudio_client = LMStudioClient()
        self.lmstudio_available = self.lmstudio_client.is_available()
        self.llm_provider_status["lmstudio"] = {
            "configured": True,
            "available": self.lmstudio_available,
            "hint": "Ensure LM Studio server is running and has a model loaded.",
        }
        if self.lmstudio_available:
            logger.info("LM Studio initialized as primary LLM")
        else:
            logger.info("LM Studio not available, will use cloud fallbacks")

        # Groq (fallback)
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key and groq_key != "PASTE_YOUR_GROQ_KEY_HERE":
            self.groq_client = Groq(api_key=groq_key)
            self.llm_provider_status["groq"] = {
                "configured": True,
                "available": True,
                "hint": "Check key validity and Groq quota/rate limits.",
            }
        else:
            self.groq_client = None
            self.llm_provider_status["groq"] = {
                "configured": False,
                "available": False,
                "hint": "Set GROQ_API_KEY in .env",
            }
        
        # Gemini (fallback)
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key and gemini_key != "PASTE_YOUR_GEMINI_KEY_HERE":
            genai.configure(api_key=gemini_key)
            gemini_model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
            self.gemini_model = genai.GenerativeModel(gemini_model_name)
            self.llm_provider_status["gemini"] = {
                "configured": True,
                "available": True,
                "hint": "Check key validity and Gemini quota/rate limits.",
            }
        else:
            self.gemini_model = None
            self.llm_provider_status["gemini"] = {
                "configured": False,
                "available": False,
                "hint": "Set GEMINI_API_KEY in .env",
            }
    
    def _call_llm(self, messages: List[Dict], tools: List[Dict] = None) -> Any:
        """Call LLM with fallback: LM Studio -> Groq -> Gemini."""
        if tools is None:
            openai_tools = self.tool_schemas_openai
            gemini_tools = [{"function_declarations": self.tool_schemas_gemini}]
            tool_choice = "auto"
            gemini_config = {"function_calling_config": "AUTO"}
        elif tools:
            openai_tools = tools
            gemini_tools = [{"function_declarations": tools}]
            tool_choice = "auto"
            gemini_config = {"function_calling_config": "AUTO"}
        else:  # tools == [] (explicitly disabled)
            openai_tools = None
            gemini_tools = None
            tool_choice = "none"
            gemini_config = {"function_calling_config": "NONE"}

        provider_errors = []
        
        # Try LM Studio first (primary - local, unlimited)
        if self.lmstudio_available and self.lmstudio_client:
            try:
                groq_messages = self._convert_to_openai_format(messages)
                kwargs = {}
                if openai_tools:
                    kwargs["tools"] = openai_tools
                    kwargs["tool_choice"] = tool_choice
                response = self.lmstudio_client.chat_completion(
                    messages=groq_messages,
                    **kwargs
                )
                logger.debug("LM Studio call successful")
                return response
            except Exception as e:
                provider_errors.append(f"LM Studio runtime error: {e}")
                logger.warning(f"LM Studio error: {e}, falling back to Groq")
        else:
            provider_errors.append("LM Studio unavailable")
        
        # Fallback to Groq
        if self.groq_client:
            try:
                groq_messages = self._convert_to_openai_format(messages)
                groq_model_name = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
                kwargs = {}
                if openai_tools:
                    kwargs["tools"] = openai_tools
                    kwargs["tool_choice"] = tool_choice
                response = self.groq_client.chat.completions.create(
                    model=groq_model_name,
                    messages=groq_messages,
                    timeout=8.0,
                    **kwargs
                )
                logger.debug("Groq call successful")
                return response
            except Exception as e:
                provider_errors.append(f"Groq runtime error: {e}")
                logger.warning(f"Groq error: {e}, falling back to Gemini")
        else:
            provider_errors.append("Groq not configured")
        
        # Fallback to Gemini - uses Gemini format tools
        if self.gemini_model:
            try:
                kwargs = {}
                if gemini_tools:
                    kwargs["tools"] = gemini_tools
                    kwargs["tool_config"] = gemini_config
                response = self.gemini_model.generate_content(
                    self._convert_to_gemini_prompt(messages),
                    request_options={"timeout": 8.0},
                    **kwargs
                )
                logger.debug("Gemini call successful")
                return response
            except Exception as e:
                provider_errors.append(f"Gemini runtime error: {e}")
                logger.warning(f"Gemini error: {e}")
        else:
            provider_errors.append("Gemini not configured")

        error_summary = " | ".join(provider_errors)
        raise RuntimeError(f"All LLMs failed. {error_summary}")
    
    def _convert_to_openai_format(self, messages: List[Dict]) -> List[Dict]:
        """Convert Gemini message format to OpenAI format."""
        converted = []
        for msg in messages:
            if isinstance(msg, str):
                converted.append({"role": "user", "content": msg})
            elif hasattr(msg, 'parts'):
                # Gemini response
                for part in msg.parts:
                    if hasattr(part, 'function_call'):
                        raw_args = getattr(part.function_call, 'args', None)
                        args_dict = dict(raw_args) if raw_args else {}
                        converted.append({
                            "role": "assistant",
                            "tool_calls": [{
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": part.function_call.name,
                                    "arguments": json.dumps(args_dict)
                                }
                            }]
                        })
                    elif hasattr(part, 'text'):
                        converted.append({"role": "assistant", "content": part.text})
            else:
                converted.append(msg)
        return converted

    def _convert_to_gemini_prompt(self, messages: List[Dict]) -> str:
        """Convert chat messages into a Gemini-friendly transcript prompt."""
        lines = []
        for msg in messages:
            if not isinstance(msg, dict):
                lines.append(str(msg))
                continue

            role = msg.get("role", "user")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")

            if content is None and tool_calls:
                for tool_call in tool_calls:
                    function = tool_call.get("function", {})
                    lines.append(
                        f"Assistant requested tool {function.get('name', 'unknown')}"
                        f"({function.get('arguments', '{}')})"
                    )
                continue

            if content is None:
                continue

            label = {
                "system": "System",
                "user": "User",
                "assistant": "Assistant",
                "tool": "Tool",
                "model": "Model",
            }.get(role, role.title())
            lines.append(f"{label}: {content}")

        return "\n\n".join(lines)
    
    def _trim_tool_result_for_llm(self, tool_name: str, result: Any) -> Any:
        """Trim massive tool outputs (like 400+ processes) to prevent LLM token overflow."""
        if not isinstance(result, dict):
            return result
        
        if tool_name == 'list_processes' and 'processes' in result:
            procs = result.get('processes', [])
            if len(procs) > 20:
                trimmed = dict(result)
                trimmed['processes'] = procs[:20]
                trimmed['total_processes'] = len(procs)
                trimmed['note'] = f"Showing top 20 processes by resource usage out of {len(procs)} total active processes."
                return trimmed
                
        if tool_name == 'list_open_ports' and 'connections' in result:
            conns = result.get('connections', [])
            if len(conns) > 20:
                trimmed = dict(result)
                trimmed['connections'] = conns[:20]
                trimmed['total_ports'] = len(conns)
                trimmed['note'] = f"Showing first 20 ports out of {len(conns)} open ports."
                return trimmed
                
        return result

    def chat(self, user_input: str) -> str:
        """Single-shot chat with tool use."""
        logger.info(f"Chat request: {user_input[:100]}")
        self.memory.add_interaction(user_input, "")
        
        # Fast-path for simple conversational greetings
        user_lower = user_input.lower().strip()
        if user_lower in ['hi', 'hii', 'hello', 'hey', 'who are you', 'help']:
            greeting = self._handle_greeting(user_lower)
            if greeting:
                self.memory.add_interaction(user_input, greeting, [])
                return greeting
        
        # Fast-path for direct destructive commands (kill process, kill port, kill highest cpu/memory)
        # Avoids hitting cloud LLM token limits and network latency for immediate response
        if any(kw in user_lower for kw in ['kill highest cpu', 'kill high cpu', 'kill cpu hog', 'kill top cpu']):
            res = self._handle_kill_highest('cpu', user_lower)
            if res:
                self.memory.add_interaction(user_input, res, [])
                return res

        if any(kw in user_lower for kw in ['kill highest memory', 'kill high memory', 'kill memory hog', 'kill top memory', 'kill highest ram', 'kill high ram']):
            res = self._handle_kill_highest('memory', user_lower)
            if res:
                self.memory.add_interaction(user_input, res, [])
                return res

        if any(kw in user_lower for kw in ['close port', 'kill port', 'stop port', 'free port', 'shut port', 'terminate port']) or (
            ('close' in user_lower or 'kill' in user_lower or 'stop' in user_lower or 'free' in user_lower) and 'port' in user_lower
        ):
            res = self._handle_close_port_request(user_lower)
            if res:
                self.memory.add_interaction(user_input, res, [])
                return res

        if self._is_kill_process_request(user_lower):
            res = self._handle_kill_by_name_or_pid(user_lower)
            if res:
                self.memory.add_interaction(user_input, res, [])
                return res

        # Build multi-turn context messages for the AI model
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        recent_history = self.memory.get_recent_context(n=4)
        for interaction in recent_history[:-1]:
            if interaction.get("user"):
                messages.append({"role": "user", "content": interaction["user"]})
            if interaction.get("agent"):
                messages.append({"role": "assistant", "content": interaction["agent"]})
        messages.append({"role": "user", "content": user_input})
        
        tool_calls = []
        max_tool_calls = 5
        
        for i in range(max_tool_calls):
            try:
                response = self._call_llm(messages)
            except RuntimeError as e:
                logger.warning(f"LLM unavailable, using local fallback: {e}")
                # Fallback to local heuristic tools when AI is unavailable or rate-limited
                local_result = self._try_local_fallback(user_input)
                if local_result:
                    self.memory.add_interaction(user_input, local_result, [])
                    return local_result
                return self._build_llm_unavailable_message(str(e))
            
            # Check for tool calls (Groq/OpenAI format)
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                if hasattr(choice, 'message') and choice.message.tool_calls:
                    # Append assistant message containing all tool calls
                    messages.append({
                        "role": "assistant",
                        "content": choice.message.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                            }
                            for tc in choice.message.tool_calls
                        ]
                    })
                    
                    # Execute each tool call and append corresponding tool response
                    for tc in choice.message.tool_calls:
                        tool_name = tc.function.name
                        args_str = tc.function.arguments
                        if args_str in ('null', '', None):
                            tool_args = {}
                        else:
                            try:
                                tool_args = json.loads(args_str)
                            except Exception:
                                tool_args = {}
                        
                        logger.info(f"Tool call #{i+1}: {tool_name}({tool_args})")
                        if tool_name == 'close_port':
                            c_port = tool_args.get('port')
                            if c_port:
                                confirm_resp = self._build_port_kill_confirmation(int(c_port))
                                if confirm_resp:
                                    return confirm_resp
                        if tool_name == 'kill_process':
                            k_pid = tool_args.get('pid')
                            if k_pid and k_pid > 1:
                                k_name = f"PID {k_pid}"
                                try:
                                    import psutil
                                    k_name = psutil.Process(k_pid).name()
                                except Exception:
                                    pass
                                confirm_data = {
                                    "type": "confirm_kill",
                                    "pid": k_pid,
                                    "name": k_name,
                                    "reason": f"kill requested by assistant for PID {k_pid}",
                                    "metric": "llm_requested"
                                }
                                return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"
                        
                        result = self._execute_tool_with_permissions(tool_name, tool_args)
                        tool_calls.append({"tool": tool_name, "args": tool_args, "result": result})
                        trimmed_result = self._trim_tool_result_for_llm(tool_name, result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(trimmed_result)
                        })
                    continue  # Continue outer loop for next turn
                
                # No tool call, return final response
                final_text = choice.message.content
                if not final_text and hasattr(choice.message, 'reasoning_content') and choice.message.reasoning_content:
                    final_text = choice.message.reasoning_content
                if not final_text:
                    final_text = str(response)
                self.memory.add_interaction(user_input, final_text, tool_calls)
                logger.info(f"Chat response: {final_text[:200]}")
                return final_text
            
            # Fallback for Gemini format response
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, 'function_call'):
                            fc = part.function_call
                            tool_name = fc.name
                            raw_args = getattr(fc, 'args', None)
                            tool_args = dict(raw_args) if raw_args else {}
                            
                            logger.info(f"Tool call (Gemini): {tool_name}({tool_args})")
                            if tool_name == 'close_port':
                                c_port = tool_args.get('port')
                                if c_port:
                                    confirm_resp = self._build_port_kill_confirmation(int(c_port))
                                    if confirm_resp:
                                        return confirm_resp
                            if tool_name == 'kill_process':
                                k_pid = tool_args.get('pid')
                                if k_pid and k_pid > 1:
                                    k_name = f"PID {k_pid}"
                                    try:
                                        import psutil
                                        k_name = psutil.Process(k_pid).name()
                                    except Exception:
                                        pass
                                    confirm_data = {
                                        "type": "confirm_kill",
                                        "pid": k_pid,
                                        "name": k_name,
                                        "reason": f"kill requested by assistant for PID {k_pid}",
                                        "metric": "llm_requested"
                                    }
                                    return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"

                            result = self._execute_tool_with_permissions(tool_name, tool_args)
                            tool_calls.append({"tool": tool_name, "args": tool_args, "result": result})
                            trimmed_result = self._trim_tool_result_for_llm(tool_name, result)
                            
                            messages.append({"role": "assistant", "content": f"Tool call: {tool_name}({tool_args})"})
                            messages.append({"role": "tool", "content": json.dumps(trimmed_result)})
                            break
                    else:
                        final_text = response.text if hasattr(response, 'text') else str(response)
                        self.memory.add_interaction(user_input, final_text, tool_calls)
                        logger.info(f"Chat response (Gemini): {final_text[:200]}")
                        return final_text
        
        logger.warning("Max tool calls reached")
        return "Max tool calls reached. Please try a more specific request."

    def _build_llm_unavailable_message(self, raw_error: str) -> str:
        """Return actionable troubleshooting guidance when all LLM providers fail."""
        status_parts = []
        for name in ["lmstudio", "groq", "gemini"]:
            state = self.llm_provider_status.get(name, {})
            configured = state.get("configured", False)
            available = state.get("available", False)
            if configured and available:
                status_parts.append(f"{name}: configured")
            elif configured and not available:
                status_parts.append(f"{name}: configured but unavailable")
            else:
                status_parts.append(f"{name}: not configured")

        status_text = ", ".join(status_parts)
        return (
            "Error: All LLM providers are currently unavailable. "
            f"Status: {status_text}. "
            "Fix: start LM Studio server, or set GROQ_API_KEY and/or GEMINI_API_KEY in .env, then restart the app. "
            f"Details: {raw_error}"
        )
    
    def _handle_greeting(self, user_lower: str) -> Optional[str]:
        """Handle friendly conversational greetings directly."""
        import re
        if re.match(r'^(?:h+[i|e|y]+|hello+|howdy|yo+|sup|greetings)(?:\s+there)?(?:[!?. ]|$)', user_lower) or any(
            user_lower == g or user_lower.startswith(g + ' ') for g in [
                'hi', 'hii', 'hiii', 'hello', 'hey', 'heyy', 'yo', 'sup', 'good morning', 
                'good evening', 'good afternoon', 'how are you', 'greetings', 'hi there', 'hello there', 'howdy'
            ]
        ):
            return "Hello! I am OS Assistant. I'm here to help you monitor system health, inspect processes, check GPU/CPU usage, and manage your computer. How can I assist you today?"

        if any(kw in user_lower for kw in ['who are you', 'what is your name', 'what are you called', 'what are you']):
            return "I am OS Assistant, your operating system management and performance companion."

        return None

    def _try_local_fallback(self, user_input: str) -> str:
        """Handle common queries locally with a natural, conversational human tone."""
        user_lower = user_input.lower().strip()

        # Friendly conversational greetings
        greeting = self._handle_greeting(user_lower)
        if greeting:
            return greeting

        # General status, health, and PC update requests
        if any(kw in user_lower for kw in [
            'update on my pc', 'pc status', 'system status', 'computer status', 'status of my pc', 
            'health check', 'system update', 'give me an update', 'how is my pc', "how's my pc", 
            'overview of my pc', 'system health', 'health', 'is my system health okay', 
            'is my pc ok', 'is my pc okay', 'system overview', 'how is the system', 'pc health'
        ]):
            cpu = execute_tool('get_cpu_usage', {})
            gpu = execute_tool('get_gpu_usage', {})
            disk = execute_tool('get_disk_usage', {'path': '/'})
            memory = execute_tool('get_top_memory_processes', {'n': 3})

            cpu_val = cpu.get('cpu_percent', 0.0) if cpu.get('success') else 0.0
            disk_val = disk.get('percent', 0.0) if disk.get('success') else 0.0
            gpu_val = gpu.get('gpu_percent') if (gpu.get('success') and gpu.get('available')) else None

            cpu_status = "Idle" if cpu_val < 30 else ("Moderate" if cpu_val < 75 else "High")
            disk_status = "Free" if disk_val < 75 else ("Warning" if disk_val < 90 else "Full")

            lines = [
                "| Resource | Usage | Graph | Status | Details |",
                "|---|---|---|---|---|",
                f"| CPU | {cpu_val:.1f}% | {_render_ascii_bar(cpu_val)} | {cpu_status} | System Load |",
            ]
            if gpu_val is not None:
                gpu_dev = gpu.get('device', 'GPU')
                lines.append(f"| GPU | {gpu_val:.1f}% | {_render_ascii_bar(gpu_val)} | Cool | {gpu_dev} |")
            
            free_gb = disk.get('free_gb', 0.0) if disk.get('success') else 0.0
            total_gb = disk.get('total_gb', 0.0) if disk.get('success') else 0.0
            lines.append(f"| Disk | {disk_val:.1f}% | {_render_ascii_bar(disk_val)} | {disk_status} | {free_gb:.1f} GB free of {total_gb:.1f} GB |")

            if memory.get('success') and memory.get('processes'):
                procs = memory['processes']
                lines.append("")
                lines.append("| Rank | PID | Top Process | CPU% | Memory |")
                lines.append("|:---:|:---:|---|:---:|:---:|")
                for idx, p in enumerate(procs[:3], 1):
                    lines.append(f"| {idx} | {p['pid']} | {p['name']} | {p.get('cpu_percent', 0.0):.1f}% | {p.get('memory_mb', 0.0):.1f} MB |")

            if cpu_val > 80.0:
                verdict = "Verdict: High CPU utilization detected."
            elif disk_val > 85.0:
                verdict = "Verdict: Disk storage is nearly full."
            else:
                verdict = "Verdict: System is cool, healthy, and running smoothly."
            lines.append(f"\n{verdict}")
            return "\n".join(lines)
        
        # Kill highest CPU process (check BEFORE generic CPU queries)
        if any(kw in user_lower for kw in ['kill highest cpu', 'kill high cpu', 'kill cpu hog', 'kill top cpu']):
            return self._handle_kill_highest('cpu', user_lower)
        
        # Kill highest memory process (check BEFORE generic memory queries)
        if any(kw in user_lower for kw in ['kill highest memory', 'kill high memory', 'kill memory hog', 'kill top memory', 'kill highest ram', 'kill high ram']):
            return self._handle_kill_highest('memory', user_lower)

        # Close / terminate listening port (check BEFORE generic process kills)
        if any(kw in user_lower for kw in ['close port', 'kill port', 'stop port', 'free port', 'shut port', 'terminate port']) or (
            ('close' in user_lower or 'kill' in user_lower or 'stop' in user_lower or 'free' in user_lower) and 'port' in user_lower
        ):
            return self._handle_close_port_request(user_lower)

        # Kill process by name or PID
        if self._is_kill_process_request(user_lower):
            return self._handle_kill_by_name_or_pid(user_lower)

        # Questions asking which process is using the most CPU/energy.
        if any(kw in user_lower for kw in [
            'which process', 'what process', 'top cpu process', 'highest cpu process',
            'taking more energy', 'using the most energy', 'using the most cpu',
            'cpu hog', 'most cpu', 'process taking the most',
            'highest energy', 'using highest energy', 'most energy'
        ]):
            result = execute_tool('list_processes', {})
            if result.get('success'):
                processes = result.get('processes', [])
                if not processes:
                    return "No active user processes found."

                top = processes[0]
                lines = [
                    "| Rank | PID | Process | CPU% | Memory |",
                    "|:---:|:---:|---|:---:|:---:|",
                ]
                for idx, p in enumerate(processes[:5], 1):
                    lines.append(f"| {idx} | {p['pid']} | {p['name']} | {p['cpu_percent']:.1f}% | {p['memory_mb']:.1f} MB |")
                lines.append(f"\nTop consumer: [bold]{top['name']}[/bold] (PID {top['pid']}).")
                return "\n".join(lines)
            return f"Error getting process list: {result.get('error')}"
        
        # Top processes by CPU (e.g., "show top 5", "now show top 5", "show top 5 processes")
        import re
        top_match = re.search(r'top\s+(\d+)', user_lower)
        if top_match and 'memory' not in user_lower and 'ram' not in user_lower:
            n = int(top_match.group(1))
            result = execute_tool('list_processes', {})
            if result.get('success'):
                processes = result.get('processes', [])[:n]
                lines = [
                    "| Rank | PID | Process | CPU% | Memory |",
                    "|:---:|:---:|---|:---:|:---:|",
                ]
                for idx, p in enumerate(processes, 1):
                    lines.append(f"| {idx} | {p['pid']} | {p['name']} | {p['cpu_percent']:.1f}% | {p['memory_mb']:.1f} MB |")
                return "\n".join(lines)
            return f"Error getting process list: {result.get('error')}"

        # CPU queries (scalar system-wide CPU percentage)
        if any(kw in user_lower for kw in ['cpu', 'processor']):
            if 'process' not in user_lower and 'top' not in user_lower and 'high' not in user_lower:
                result = execute_tool('get_cpu_usage', {})
                if result.get('success'):
                    p = result['cpu_percent']
                    desc = "Idle" if p < 30 else ("Moderate" if p < 75 else "High Load")
                    lines = [
                        "| Metric | Usage | Graph | Status |",
                        "|---|---|---|---|",
                        f"| CPU Utilization | {p:.1f}% | {_render_ascii_bar(p)} | {desc} |",
                    ]
                    return "\n".join(lines)
                return f"Error getting CPU: {result.get('error')}"
        
        # Memory queries
        if any(kw in user_lower for kw in ['memory', 'ram', 'mem']):
            n = 5
            import re
            match = re.search(r'top\s+(\d+)', user_lower)
            if match:
                n = int(match.group(1))
            result = execute_tool('get_top_memory_processes', {'n': n})
            if result.get('success'):
                processes = result.get('processes', [])
                if not processes:
                    return "No memory-intensive processes found."
                lines = [
                    "| Rank | PID | Process | Memory | CPU% |",
                    "|:---:|:---:|---|:---:|:---:|",
                ]
                for idx, p in enumerate(processes, 1):
                    lines.append(f"| {idx} | {p['pid']} | {p['name']} | {p['memory_mb']:.1f} MB | {p['cpu_percent']:.1f}% |")
                return "\n".join(lines)
            return f"Error getting memory info: {result.get('error')}"
        
        # Disk queries
        if any(kw in user_lower for kw in ['disk', 'storage', 'space']):
            result = execute_tool('get_disk_usage', {'path': '/'})
            if result.get('success'):
                free_gb = result['free_gb']
                total_gb = result['total_gb']
                used_gb = result['used_gb']
                pct = result['percent']
                tip = "Plenty of free storage" if pct < 75 else "Storage nearly full"
                lines = [
                    "| Mount | Used | Total | Free | Graph | Status |",
                    "|---|---|---|---|---|---|",
                    f"| Primary (/) | {used_gb:.1f} GB ({pct:.1f}%) | {total_gb:.1f} GB | {free_gb:.1f} GB | {_render_ascii_bar(pct)} | {tip} |",
                ]
                return "\n".join(lines)
            return f"Error getting disk usage: {result.get('error')}"
        
        # GPU queries
        if any(kw in user_lower for kw in ['gpu', 'graphics', 'vram', 'video card', 'graphic card']):
            result = execute_tool('get_gpu_usage', {})
            if result.get('success'):
                if result.get('available'):
                    mem_parts = []
                    if result.get('memory_used_mb') is not None and result.get('memory_total_mb') is not None:
                        mem_parts.append(f"{result['memory_used_mb']:.1f} MB / {result['memory_total_mb']:.1f} MB")
                    elif result.get('memory_used_mb') is not None:
                        mem_parts.append(f"{result['memory_used_mb']:.1f} MB used")
                    vram_str = mem_parts[0] if mem_parts else "Unified Memory"
                    lines = [
                        "| Device | Utilization | Graph | VRAM Details |",
                        "|---|---|---|---|",
                        f"| {result.get('device', 'GPU')} | {result['gpu_percent']:.1f}% | {_render_ascii_bar(result['gpu_percent'])} | {vram_str} |",
                    ]
                    return "\n".join(lines)
                else:
                    return "Dedicated GPU telemetry is not accessible on this system configuration."
            return f"Error getting GPU usage: {result.get('error')}"

        # Process list
        if any(kw in user_lower for kw in ['process', 'running']):
            if 'list' in user_lower or 'show' in user_lower:
                result = execute_tool('list_processes', {})
                if result.get('success'):
                    processes = result.get('processes', [])[:10]
                    lines = [
                        "| Rank | PID | Process | CPU% | Memory |",
                        "|:---:|:---:|---|:---:|:---:|",
                    ]
                    for idx, p in enumerate(processes, 1):
                        lines.append(f"| {idx} | {p['pid']} | {p['name']} | {p['cpu_percent']:.1f}% | {p['memory_mb']:.1f} MB |")
                    return "\n".join(lines)
        # Network bandwidth & speed queries
        if any(kw in user_lower for kw in ['bandwidth', 'network speed', 'download speed', 'upload speed', 'internet speed', 'net speed', 'throughput', 'data transferred', 'bytes received', 'network bandwidth']):
            result = execute_tool('get_network_bandwidth', {'interval': 0.3})
            if result.get('success'):
                down_spd = result.get('download_speed', '0 B/s')
                up_spd = result.get('upload_speed', '0 B/s')
                tot_rx = result.get('total_received', '0 B')
                tot_tx = result.get('total_sent', '0 B')
                lines = [
                    "| Direction | Current Speed | Total Transferred | Status |",
                    "|---|---|---|---|",
                    f"| Download (RX) | {down_spd} | {tot_rx} | Active |",
                    f"| Upload (TX)   | {up_spd} | {tot_tx} | Active |",
                    "\nLive network throughput measured over 300ms window."
                ]
                return "\n".join(lines)
            return f"Error measuring network bandwidth: {result.get('error')}"

        # Process inspection by PID
        if 'inspect' in user_lower and ('pid' in user_lower or 'proc' in user_lower or re.search(r'\b\d+\b', user_lower)):
            pid_match = re.search(r'\b(\d+)\b', user_lower)
            if pid_match:
                pid = int(pid_match.group(1))
                try:
                    import psutil
                    proc = psutil.Process(pid)
                    p_name = proc.name()
                    p_cpu = proc.cpu_percent(interval=0.1)
                    p_mem = proc.memory_info().rss / (1024 * 1024)
                    p_status = proc.status()
                    lines = [
                        f"Process Telemetry for PID {pid} (`{p_name}`):",
                        "",
                        "| Metric | Value | Graph / Detail |",
                        "|---|---|---|",
                        f"| PID | {pid} | System Process Identifier |",
                        f"| Name | {p_name} | Executable binary |",
                        f"| Status | {p_status} | Operational state |",
                        f"| CPU% | {p_cpu:.1f}% | {_render_ascii_bar(min(100.0, p_cpu))} |",
                        f"| Memory | {p_mem:.1f} MB | Physical resident set size |",
                    ]
                    return "\n".join(lines)
                except Exception as e:
                    return f"Cannot inspect PID {pid}: process may have already exited ({e})."

        # Port queries
        if any(kw in user_lower for kw in ['port', 'network', 'connection']):
            result = execute_tool('list_open_ports', {})
            if result.get('success'):
                connections = result.get('connections', [])
                if not connections:
                    return "No open listening network ports found."
                lines = [
                    f"Found {len(connections)} active listening network ports:",
                    "",
                    "| Port | Protocol | PID | Process Name |",
                    "|------|----------|-----|--------------|",
                ]
                for c in connections[:12]:
                    lines.append(f"| {c['port']} | {c['protocol']} | {c['pid']} | `{c['process']}` |")
                if len(connections) > 12:
                    lines.append(f"\n... and {len(connections) - 12} additional listening ports.")
                return "\n".join(lines)
            return f"Error listing ports: {result.get('error')}"
        
        return None  # No local handler for this query

    def _build_port_kill_confirmation(self, port: int) -> Optional[str]:
        """Find listening processes on port and return structured kill confirmation."""
        import subprocess
        import json
        import psutil
        pids = []
        proc_names = []
        seen = set()

        # 1. Check psutil network connections
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.status == 'LISTEN' and conn.laddr.port == port and conn.pid:
                    if conn.pid > 1 and conn.pid not in seen:
                        seen.add(conn.pid)
                        pids.append(conn.pid)
                        try:
                            proc_names.append(psutil.Process(conn.pid).name())
                        except Exception:
                            proc_names.append(f"PID {conn.pid}")
        except Exception:
            pass

        # 2. Check lsof fallback (non-root on macOS/Linux)
        if not pids:
            try:
                res = subprocess.run(
                    ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-n", "-P"],
                    capture_output=True,
                    text=True,
                    timeout=1.5
                )
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.strip().splitlines()[1:]:
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            pid = int(parts[1])
                            if pid > 1 and pid not in seen:
                                seen.add(pid)
                                pids.append(pid)
                                proc_names.append(parts[0])
            except Exception:
                pass

        if not pids:
            return None

        confirm_data = {
            "type": "confirm_kill",
            "port": port,
            "pids": pids,
            "pid": pids[0],
            "name": f"{', '.join(proc_names)} on port {port}",
            "reason": f"close port {port}",
            "metric": "port_close"
        }
        return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"

    def _handle_close_port_request(self, user_lower: str) -> str:
        """Handle request to close or terminate process on an open network port."""
        import re
        match = re.search(r'(?:close|kill|stop|free|shut|terminate)\s*(?:port\s*)?:?\s*(\d{1,5})', user_lower)
        if not match:
            match = re.search(r'port\s+(\d{1,5})', user_lower)

        if match:
            port = int(match.group(1))
            confirm_resp = self._build_port_kill_confirmation(port)
            if confirm_resp:
                return confirm_resp
            return f"No active listening process found on port {port}. The port is already closed."

        result = execute_tool('list_open_ports', {})
        connections = result.get('connections', []) if result.get('success') else []
        if not connections:
            return "No open listening network ports found."

        lines = [
            "Currently open listening network ports:",
            "",
            "| Port | Protocol | PID | Process |",
            "|:---:|:---:|:---:|---|",
        ]
        for c in connections[:8]:
            lines.append(f"| {c['port']} | {c.get('protocol', 'TCP')} | {c['pid']} | {c['process']} |")
        lines.append("")
        lines.append("Which port would you like to close? (e.g. type 'close port 5000' or click 🔌 Close Port).")
        return "\n".join(lines)
    
    def _handle_kill_highest(self, metric: str, user_input: str) -> str:
        """Handle kill highest CPU/memory process with confirmation."""
        import json
        
        if metric == 'cpu':
            result = execute_tool('list_processes', {})
            if not result.get('success'):
                return f"Error getting processes: {result.get('error')}"
            processes = result.get('processes', [])
            # Sort by CPU, filter system processes (pid > 1) and non-zero CPU
            candidates = [p for p in processes if p['pid'] > 1 and (p.get('cpu_percent') or 0) > 0.1]
            if not candidates:
                return "No user processes with significant CPU usage found."
            target = max(candidates, key=lambda x: x.get('cpu_percent') or 0)
            metric_value = f"{target.get('cpu_percent', 0):.1f}% CPU"
        else:  # memory
            result = execute_tool('get_top_memory_processes', {'n': 10})
            if not result.get('success'):
                return f"Error getting memory processes: {result.get('error')}"
            processes = result.get('processes', [])
            candidates = [p for p in processes if p['pid'] > 1 and p['memory_mb'] > 10]
            if not candidates:
                return "No user processes with significant memory usage found."
            target = max(candidates, key=lambda x: x['memory_mb'])
            metric_value = f"{target['memory_mb']:.1f} MB"
        
        # Return structured confirmation request for TUI
        confirm_data = {
            "type": "confirm_kill",
            "pid": target['pid'],
            "name": target['name'],
            "reason": f"highest {metric} ({metric_value})",
            "metric": metric
        }
        return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"
    
    def _is_kill_process_request(self, user_input: str) -> bool:
        """Check if user is asking to kill a specific process by name or PID."""
        import re
        stripped = user_input.strip()
        # Direct PID input like "PID : 25497", "pid 25497", or "kill pid 25497"
        if re.search(r'^(?:(?:kill|terminate|stop|close|end)(?:\s+all)?\s+)?pid\s*[:=]?\s*\d+', stripped, re.IGNORECASE):
            return True
        # Raw integer PID when previous agent response asked to specify PID or user types a number
        if re.search(r'^\d+$', stripped):
            recent = self.memory.get_recent_context(n=3)
            for item in reversed(recent[:-1]):
                prev = item.get("agent", "").lower()
                if "please specify pid" in prev or "pid" in prev or "multiple processes match" in prev or "kill" in prev:
                    return True

        kill_patterns = [
            r'^(?:kill|terminate|stop\s+process|close\s+process|end\s+process)\s+.+',
        ]
        for pattern in kill_patterns:
            if re.search(pattern, stripped, re.IGNORECASE):
                # Exclude "kill highest" patterns already handled
                if 'highest' not in stripped and 'high cpu' not in stripped and 'high mem' not in stripped and 'top cpu' not in stripped and 'top mem' not in stripped:
                    return True
        return False
    
    def _handle_kill_by_name_or_pid(self, user_input: str) -> str:
        """Handle kill process by name or PID with confirmation."""
        import re
        import json
        
        user_input = user_input.strip()
        target_pid = None
        target_name = None
        reason = ""
        
        # Check for pure digits (e.g. "25497", "kill 25497", "PID : 25497")
        raw_num_match = re.search(r'^(?:(?:kill|terminate|stop\s+process|close\s+process|end\s+process)\s+)?(?:pid\s*[:=]?\s*)?(\d+)$', user_input, re.IGNORECASE)
        if raw_num_match:
            target_pid = int(raw_num_match.group(1))
            reason = f"PID {target_pid}"
        else:
            pid_match = re.search(r'pid\s*[:=]?\s*(\d+)', user_input, re.IGNORECASE)
            if pid_match:
                target_pid = int(pid_match.group(1))
                reason = f"PID {target_pid}"
            else:
                name_match = re.search(r'^(?:kill\s+all|terminate\s+all|kill|terminate|stop\s+process|close\s+process|end\s+process)\s+(?:process\s+)?(.+)', user_input, re.IGNORECASE)
                if name_match:
                    target_name = name_match.group(1).strip().lower()
                    reason = f"name '{target_name}'"
                else:
                    target_name = user_input.lower()
                    reason = f"name '{target_name}'"
        
        # Get process list
        result = execute_tool('list_processes', {})
        if not result.get('success'):
            return f"Error getting processes: {result.get('error')}"
        
        processes = result.get('processes', [])
        
        if target_pid:
            target = next((p for p in processes if p['pid'] == target_pid), None)
            if not target:
                try:
                    import psutil
                    proc = psutil.Process(target_pid)
                    target = {'pid': target_pid, 'name': proc.name()}
                except Exception:
                    return f"No running process found with PID {target_pid}"
            
            if target['pid'] <= 1:
                return f"Cannot kill system process PID {target['pid']} ({target['name']})"
            
            confirm_data = {
                "type": "confirm_kill",
                "pid": target['pid'],
                "name": target['name'],
                "reason": f"requested kill by {reason}",
                "metric": "user_requested"
            }
            return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"
        
        elif target_name:
            # Look for matching processes
            matches = [p for p in processes if target_name in p['name'].lower()]
            if not matches:
                # Clean search (e.g. without spaces or hyphens)
                cleaned = target_name.replace(" ", "").replace("-", "")
                matches = [p for p in processes if cleaned in p['name'].lower().replace(" ", "").replace("-", "")]
            
            if not matches:
                return f"No running process found matching '{target_name}'"
            
            # Check if user asked to kill all
            is_kill_all = bool(re.search(r'\b(all|every)\b', user_input, re.IGNORECASE))
            valid_matches = [p for p in matches if p['pid'] > 1]
            if not valid_matches:
                return f"Cannot kill critical system process matching '{target_name}'"
            
            if is_kill_all:
                valid_pids = [p['pid'] for p in valid_matches]
                confirm_data = {
                    "type": "confirm_kill",
                    "pids": valid_pids,
                    "pid": valid_pids[0],
                    "name": f"all {len(valid_pids)} processes matching '{target_name}'",
                    "reason": f"kill all {len(valid_pids)} processes matching '{target_name}'",
                    "metric": "user_requested_all"
                }
                return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"
            
            # If single match or exact match exists:
            exact_matches = [p for p in valid_matches if p['name'].lower() == target_name]
            if len(exact_matches) == 1:
                target = exact_matches[0]
            elif len(exact_matches) > 1:
                target = max(exact_matches, key=lambda x: (x.get('memory_mb', 0) or 0) + (x.get('cpu_percent', 0) or 0))
            elif len(valid_matches) == 1:
                target = valid_matches[0]
            else:
                # Multiple partial matches, pick the main/highest resource one
                target = max(valid_matches, key=lambda x: (x.get('memory_mb', 0) or 0) + (x.get('cpu_percent', 0) or 0))
            
            helper_count = len(valid_matches) - 1
            extra_tip = f" (Note: {len(valid_matches)} matching processes found. Type 'kill all {target_name}' to kill all)" if helper_count > 0 else ""
            confirm_data = {
                "type": "confirm_kill",
                "pid": target['pid'],
                "name": target['name'],
                "reason": f"kill {target['name']}{extra_tip}",
                "metric": "user_requested"
            }
            return f"__CONFIRM_KILL__{json.dumps(confirm_data)}__"

    def _execute_tool_with_permissions(self, tool_name: str, params: Dict[str, Any], confirmed: bool = False) -> Dict[str, Any]:
        """Execute a tool with tier-based permissions and audit logging."""
        tier = get_tool_tier(tool_name)
        
        # Check circuit breaker for destructive actions
        if tier == "destructive" and self.circuit_breaker.is_triggered():
            return {"success": False, "error": "Circuit breaker triggered: too many destructive actions. Please wait."}
        
        # Get confirmation if needed and not already confirmed
        if not confirmed and not confirm_action(tier, tool_name, params):
            return {"success": False, "error": "User cancelled or confirmation required"}
        
        # Log action before execution
        log_id = log_action(tool_name, params, tier, None)
        
        try:
            # Execute the tool
            result = execute_tool(tool_name, params)
            
            # Derive undo_data from result or fallback params
            undo_data = None
            if isinstance(result, dict):
                undo_data = result.get("undo_data")
            if not undo_data and tier in ["reversible", "destructive"]:
                if tool_name == "pause_process":
                    undo_data = {"pid": params.get("pid"), "action": "pause_process"}
                elif tool_name == "resume_process":
                    undo_data = {"pid": params.get("pid"), "action": "resume_process"}
                elif tool_name == "set_priority":
                    undo_data = {"pid": params.get("pid")}
                elif tool_name == "stop_service":
                    undo_data = {"service": params.get("name")}
            
            # Update audit log with result and undo_data
            update_result(log_id, result, undo_data=undo_data)
            
            # Record in circuit breaker if destructive
            if tier == "destructive" and result.get("success"):
                self.circuit_breaker.record_action()
            
            return result
        except Exception as e:
            error_result = {"success": False, "error": str(e)}
            update_result(log_id, error_result)
            return error_result
    
    def run_goal(self, goal: str, max_steps: int = 10, dry_run: bool = False) -> str:
        """Multi-step goal execution (Plan -> Act -> Observe -> Reflect)."""
        self.memory.add_interaction(goal, "")
        
        # Planning phase
        plan = self._create_plan(goal)
        
        if dry_run:
            plan_text = "Plan (dry run):\n"
            for i, step in enumerate(plan.get("steps", []), 1):
                plan_text += f"  {i}. {step['tool']}({step['params']}) - {step.get('reason', '')}\n"
            return plan_text
        
        # Execute plan with reflection
        return self._execute_plan(goal, plan, max_steps)
    
    def _create_plan(self, goal: str) -> Dict[str, Any]:
        """Create a multi-step plan for the goal."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": PLANNING_PROMPT + "\n\nGoal: " + goal}
        ]
        
        # Pass empty tools list to prevent tool calling during planning
        response = self._call_llm(messages, tools=[])
        
        try:
            # Parse plan from response
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                if hasattr(choice, 'message'):
                    msg = choice.message
                    plan_text = msg.content or getattr(msg, 'reasoning_content', None) or str(response)
                else:
                    plan_text = str(response)
            elif hasattr(response, 'text'):
                plan_text = response.text
            else:
                plan_text = str(response)
            
            # Extract JSON from response
            import re
            json_match = re.search(r'\{.*\}', plan_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        
        return {"steps": []}
    
    def _execute_plan(self, goal: str, plan: Dict, max_steps: int) -> str:
        """Execute plan with reflection loop."""
        steps = plan.get("steps", [])
        executed_steps = []
        
        for i, step in enumerate(steps[:max_steps]):
            tool_name = step.get("tool")
            params = step.get("params", {})
            reason = step.get("reason", "")
            
            # Execute step
            result = self._execute_tool_with_permissions(tool_name, params)
            executed_steps.append({"step": step, "result": result})
            
            # Reflection: decide whether to continue
            reflection = self._reflect(goal, executed_steps, i == len(steps) - 1)
            
            if reflection.get("decision") == "done":
                return reflection.get("summary", "Goal completed.")
            elif reflection.get("decision") == "error":
                return f"Error: {reflection.get('summary', 'Unknown error')}"
            # Continue to next step
        
        return f"Completed {len(executed_steps)} steps. Goal may need more steps."
    
    def _reflect(self, goal: str, executed_steps: List[Dict], is_last_step: bool) -> Dict[str, Any]:
        """Reflect on progress and decide next action."""
        # Create a concise summary of executed steps to avoid context overflow
        steps_summary = []
        for s in executed_steps:
            step = s.get('step', {})
            result = s.get('result', {})
            tool = step.get('tool', 'unknown')
            success = result.get('success', False)
            steps_summary.append(f"{tool}: {'ok' if success else 'failed'}")
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": 
                REFLECTION_PROMPT + 
                f"\n\nGoal: {goal}" +
                f"\n\nExecuted: {'; '.join(steps_summary)}" +
                f"\n\nIs last planned step: {is_last_step}"
            }
        ]
        
        # Use local fallback for reflection to avoid LLM rate limits/context issues
        local_result = self._try_local_fallback(f"reflect on goal: {goal}, steps: {steps_summary}, last: {is_last_step}")
        if local_result and not local_result.startswith("__CONFIRM_KILL__"):
            # For reflection, just return a simple decision
            if is_last_step or "failed" in str(steps_summary).lower():
                return {"decision": "done", "summary": f"Completed {len(executed_steps)} steps."}
            return {"decision": "continue", "summary": "Continue to next step."}
        
        response = self._call_llm(messages, tools=[])
        
        try:
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                if hasattr(choice, 'message'):
                    msg = choice.message
                    reflection_text = msg.content or getattr(msg, 'reasoning_content', None) or str(response)
                else:
                    reflection_text = str(response)
            elif hasattr(response, 'text'):
                reflection_text = response.text
            else:
                reflection_text = str(response)
            
            import re
            json_match = re.search(r'\{.*\}', reflection_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        
        return {"decision": "continue" if not is_last_step else "done", "summary": "Completed."}
    
    def undo_last(self) -> str:
        """Undo the last action from audit log."""
        last_action = get_last_action(reversible_only=True)
        if not last_action:
            return "No reversible or destructive actions to undo."
        
        action = last_action['action']
        raw_undo = last_action.get('undo_data')
        undo_data = json.loads(raw_undo) if raw_undo else {}
        params = json.loads(last_action.get('params', '{}')) if last_action.get('params') else {}
        
        # Perform undo based on action type
        if action == "pause_process":
            pid = undo_data.get("pid") or params.get("pid")
            if not pid:
                return "Cannot undo pause: missing PID"
            result = execute_tool("resume_process", {"pid": pid})
        elif action == "resume_process":
            pid = undo_data.get("pid") or params.get("pid")
            if not pid:
                return "Cannot undo resume: missing PID"
            result = execute_tool("pause_process", {"pid": pid})
        elif action == "set_priority":
            pid = undo_data.get("pid") or params.get("pid")
            prev_nice = undo_data.get("previous_nice")
            if prev_nice is None:
                return f"Cannot revert priority for PID {pid}: previous nice value unknown"
            result = execute_tool("set_priority", {"pid": pid, "nice": prev_nice})
        elif action == "delete_file":
            dest_path = undo_data.get("path") or params.get("path")
            backup_path = undo_data.get("backup_path")
            was_dir = undo_data.get("was_dir", False)
            if not backup_path or not os.path.exists(backup_path):
                return f"Cannot restore {dest_path}: backup not found in trash"
            try:
                import shutil
                if was_dir:
                    shutil.copytree(backup_path, dest_path)
                else:
                    shutil.copy2(backup_path, dest_path)
                result = {"success": True, "message": f"Restored {dest_path} from trash"}
            except Exception as e:
                result = {"success": False, "error": str(e)}
        elif action == "stop_service":
            service_name = undo_data.get("service") or params.get("name")
            if not service_name:
                return "Cannot restart service: missing service name"
            import platform, subprocess
            sys_name = platform.system()
            try:
                if sys_name == "Linux":
                    res = subprocess.run(["systemctl", "start", service_name], capture_output=True, text=True)
                elif sys_name == "Darwin":
                    res = subprocess.run(["launchctl", "start", service_name], capture_output=True, text=True)
                else:
                    res = None
                if res and res.returncode == 0:
                    result = {"success": True, "message": f"Restarted service {service_name}"}
                else:
                    err = res.stderr if res else f"Unsupported OS {sys_name}"
                    result = {"success": False, "error": err}
            except Exception as e:
                result = {"success": False, "error": str(e)}
        elif action in ["kill_process", "close_port"]:
            # Cannot truly undo kill, but can try to restart if cmdline exists
            cmdline = undo_data.get("cmdline", [])
            if not cmdline and undo_data.get("undo_list"):
                cmdline = undo_data["undo_list"][0].get("cmdline", [])
            if cmdline:
                import subprocess
                try:
                    subprocess.Popen(cmdline, cwd=undo_data.get("cwd"))
                    result = {"success": True, "message": f"Restarted process: {cmdline}"}
                except Exception as e:
                    result = {"success": False, "error": str(e)}
            else:
                pids = undo_data.get("terminated_pids") or [undo_data.get("pid")]
                port = undo_data.get("port")
                target = f"port {port} (PIDs: {pids})" if port else f"PIDs: {pids}"
                result = {"success": False, "error": f"Process on {target} cannot be automatically restarted without executable path"}
        else:
            return f"Undo not implemented for {action}"
        
        if result.get("success"):
            mark_undone(last_action['id'])
            return f"Undid {action}: {result.get('message', 'success')}"
        else:
            return f"Undo failed: {result.get('error', 'unknown error')}"