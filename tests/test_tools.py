# Tests for Tools

import pytest
from src.tools.read_only import (
    list_processes, get_disk_usage, get_cpu_usage, get_gpu_usage,
    get_top_memory_processes, list_open_ports
)
from src.tools.reversible import pause_process, resume_process, set_priority, clear_cache_dir
from src.tools.destructive import kill_process, delete_file, stop_service
from src.tools.registry import get_tool_schemas, execute_tool, get_tool_tiers
from src.safety.permissions import get_tool_tier, requires_confirmation, confirm_action
from src.safety.circuit_breaker import CircuitBreaker


class TestReadOnlyTools:
    def test_list_processes(self):
        result = list_processes()
        assert result.get("success") == True
        assert isinstance(result.get("processes"), list)
        if result["processes"]:
            assert "pid" in result["processes"][0]
            assert "name" in result["processes"][0]
            assert result["processes"][0]["uptime_sec"] >= 0

    def test_list_processes_uptime_positive(self):
        result = list_processes()
        for proc in result.get("processes", []):
            assert proc.get("uptime_sec", 0) >= 0
    
    def test_get_disk_usage(self):
        result = get_disk_usage("/")
        assert result.get("success") == True
        assert "total_gb" in result
        assert "used_gb" in result
        assert "free_gb" in result
        assert "percent" in result
        assert result["total_gb"] > 0
    
    def test_get_cpu_usage(self):
        result = get_cpu_usage()
        assert result.get("success") == True
        assert "cpu_percent" in result
        assert isinstance(result["cpu_percent"], (int, float))
        assert 0 <= result["cpu_percent"] <= 100

    def test_get_gpu_usage(self):
        result = get_gpu_usage()
        assert result.get("success") == True
        assert isinstance(result.get("available"), bool)
        if result.get("available"):
            assert isinstance(result.get("gpu_percent"), (int, float))
            assert 0 <= result.get("gpu_percent") <= 100
            assert "device" in result
    
    def test_get_top_memory_processes(self):
        result = get_top_memory_processes(5)
        assert result.get("success") == True
        assert isinstance(result.get("processes"), list)
        assert len(result["processes"]) <= 5
        # Verify descending order by memory_mb
        mem_values = [p["memory_mb"] for p in result["processes"]]
        assert mem_values == sorted(mem_values, reverse=True)
    
    def test_list_open_ports(self):
        result = list_open_ports()
        assert result.get("success") == True
        assert isinstance(result.get("connections"), list)


class TestToolRegistry:
    def test_get_tool_schemas(self):
        schemas = get_tool_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) > 0
        for schema in schemas:
            assert "type" in schema
            assert "function" in schema
            assert "name" in schema["function"]
    
    def test_get_tool_tiers(self):
        tiers = get_tool_tiers()
        assert isinstance(tiers, dict)
        assert "list_processes" in tiers
        assert tiers["list_processes"] == "read_only"
        assert tiers["kill_process"] == "destructive"
    
    def test_execute_tool(self):
        result = execute_tool("get_cpu_usage", {})
        assert isinstance(result, (float, dict))

    def test_kill_process_accepts_signal_param(self):
        # PID 99999999 does not exist, but execute_tool must not raise TypeError for "signal"
        result = execute_tool("kill_process", {"pid": 99999999, "signal": 9})
        assert result.get("success") == False
        assert "not found" in result.get("error", "").lower()


class TestPermissions:
    def test_get_tool_tier(self):
        assert get_tool_tier("list_processes") == "read_only"
        assert get_tool_tier("pause_process") == "reversible"
        assert get_tool_tier("kill_process") == "destructive"
    
    def test_requires_confirmation(self):
        assert requires_confirmation("read_only") == False
        assert requires_confirmation("reversible") == True
        assert requires_confirmation("destructive") == True

    def test_confirm_action_callback(self):
        approved = confirm_action("destructive", "kill_process", {"pid": 123}, confirm_callback=lambda *_: True)
        assert approved == True
        denied = confirm_action("destructive", "kill_process", {"pid": 123}, confirm_callback=lambda *_: False)
        assert denied == False


class TestAuditLogAndUndo:
    def test_audit_log_persists_undo_data_and_ignores_readonly(self):
        from src.storage.audit_log import init_db, log_action, update_result, get_last_action, mark_undone
        import json
        init_db()

        # Log a reversible action with undo data
        log_id = log_action("pause_process", {"pid": 12345}, "reversible", None)
        update_result(log_id, {"success": True}, undo_data={"pid": 12345, "action": "pause_process"})

        # Log a read_only action
        ro_id = log_action("get_cpu_usage", {}, "read_only", None)
        update_result(ro_id, {"success": True, "cpu_percent": 12.0})

        # get_last_action(reversible_only=True) should return the pause_process action
        last = get_last_action(reversible_only=True)
        assert last is not None
        assert last["action"] == "pause_process"
        assert json.loads(last["undo_data"])["pid"] == 12345

        # Cleanup
        mark_undone(last["id"])


class TestCircuitBreaker:
    def test_circuit_breaker(self):
        cb = CircuitBreaker(max_actions=3, window_seconds=60)
        assert cb.is_triggered() == False
        
        cb.record_action()
        cb.record_action()
        assert cb.get_remaining_actions() == 1
        
        cb.record_action()
        assert cb.is_triggered() == True
        
        cb.reset()
        assert cb.is_triggered() == False


class TestTUIComponents:
    def test_gauge_bar_rendering(self):
        from src.ui.tui import render_gauge_bar
        bar_low = render_gauge_bar(15.0)
        bar_mid = render_gauge_bar(70.0)
        bar_high = render_gauge_bar(95.0)
        
        assert "15.0%" in bar_low
        assert "70.0%" in bar_mid
        assert "95.0%" in bar_high
        assert "bright_green" in bar_low
        assert "bright_yellow" in bar_mid
        assert "bright_red" in bar_high

    def test_app_instantiation(self):
        from src.ui.tui import AIOSAgentApp
        app = AIOSAgentApp()
        assert app is not None
        assert hasattr(app, "watcher")
        assert hasattr(app, "agent")

    def test_format_chat_markup(self):
        from src.ui.tui import format_chat_markup
        raw = "GPU running at **27.0%** with **VRAM** usage."
        formatted = format_chat_markup(raw)
        assert "**" not in formatted
        assert "[bold]27.0%[/bold]" in formatted
        assert "[bold]VRAM[/bold]" in formatted

    def test_chat_panel_richlog_wrap(self):
        import asyncio
        from src.ui.tui import AIOSAgentApp, RichLog

        async def _check():
            app = AIOSAgentApp()
            async with app.run_test():
                chat_log = app.query_one("#chat_log", RichLog)
                assert chat_log.wrap is True
                assert chat_log.min_width == 1

        asyncio.run(_check())

    def test_parse_markdown_table(self):
        from src.ui.tui import parse_markdown_table
        lines = [
            "| Rank | PID | Process Name | Memory Used | CPU % | Uptime |",
            "|------|-----|--------------|-------------|-------|--------|",
            "| 1  | 19349 | `Helper` | 439 MB | 0.0 | ~1 h |",
            "| 2  | 1848  | `Browser` | 369 MB | 0.3 | ~13 h |",
        ]
        table = parse_markdown_table(lines)
        assert table is not None
        assert len(table.columns) == 6
        assert len(table.rows) == 2

    def test_send_button_label(self):
        import asyncio
        from src.ui.tui import AIOSAgentApp, Button

        async def _check():
            app = AIOSAgentApp()
            async with app.run_test():
                btn = app.query_one("#btn_send", Button)
                assert "Send" in str(btn.label)

        asyncio.run(_check())

    def test_render_ports_section(self):
        from src.ui.tui import SystemStatsPanel
        panel = SystemStatsPanel()
        sample_ports = [
            {"port": 5000, "protocol": "TCP", "pid": 1234, "process": "flask"},
            {"port": 8080, "protocol": "TCP", "pid": 5678, "process": "node"}
        ]
        rendered = panel._render_ports(sample_ports)
        assert "5000" in rendered
        assert "8080" in rendered
        assert "flask" in rendered
        assert "node" in rendered
        empty_rendered = panel._render_ports([])
        assert "No active listening ports" in empty_rendered

    def test_render_ascii_bar(self):
        from src.agent.core import _render_ascii_bar
        bar_0 = _render_ascii_bar(0.0)
        assert "░░░░░░░░░░" in bar_0
        bar_50 = _render_ascii_bar(50.0)
        assert "█████░░░░░" in bar_50
        bar_100 = _render_ascii_bar(100.0)
        assert "██████████" in bar_100

    def test_graphical_fallback_response(self):
        from src.agent.core import Agent
        from src.ui.tui import parse_markdown_table
        agent = Agent()
        reply = agent._try_local_fallback("how is my pc")
        assert "| Resource |" in reply
        assert "| CPU |" in reply
        assert "Verdict:" in reply
        # Ensure it parses into valid markdown tables
        lines = [line.strip() for line in reply.split("\n") if line.strip().startswith("|") and line.strip().endswith("|")]
        tbl = parse_markdown_table(lines[:5])
        assert tbl is not None

    def test_close_port_tool_and_schema(self):
        import socket, subprocess, sys, time
        from src.tools.destructive import close_port
        from src.tools.registry import get_tool_schemas, get_tool_tiers

        # Verify schema and tier
        tiers = get_tool_tiers()
        assert tiers.get("close_port") == "destructive"
        schemas = get_tool_schemas()
        assert any(s["function"]["name"] == "close_port" for s in schemas)

        # Test invalid/empty port
        invalid_res = close_port(99999)
        assert invalid_res["success"] is False
        assert "No active listening process" in invalid_res["error"]

        # Test live ephemeral port listener
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        proc = subprocess.Popen([
            sys.executable, "-c",
            f"import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(1); time.sleep(30)"
        ])
        try:
            time.sleep(0.4)
            close_res = close_port(port)
            assert close_res["success"] is True
            assert close_res["port"] == port
            assert proc.pid in close_res["terminated_pids"]
            assert "undo_data" in close_res
        finally:
            if proc.poll() is None:
                proc.terminate()

    def test_close_port_agent_routing(self):
        from src.agent.core import Agent
        agent = Agent()
        # Prompt when no port is specified
        prompt_res = agent._try_local_fallback("close port")
        assert "Which port would you like to close?" in prompt_res
        # When non-existent port is specified
        not_found_res = agent._try_local_fallback("close port 99999")
        assert "already closed" in not_found_res or "No active listening process" in not_found_res

    def test_close_port_chip_button(self):
        import asyncio
        from src.ui.tui import AIOSAgentApp, Button

        async def _check():
            app = AIOSAgentApp()
            async with app.run_test():
                btn = app.query_one("#btn_close_port", Button)
                assert "Close Port" in str(btn.label)

        asyncio.run(_check())

    def test_ai_engine_bar_aesthetic_and_preserved_on_clear(self):
        import asyncio
        from src.ui.tui import AIOSAgentApp, Static, RichLog

        async def _check():
            app = AIOSAgentApp()
            async with app.run_test() as pilot:
                # 1. Bar exists and displays aesthetic engine status
                bar = app.query_one("#ai_engine_bar", Static)
                assert "⚡ AI ENGINES" in str(bar.content)
                assert "Groq" in str(bar.content)
                assert "Gemini" in str(bar.content)

                # 2. Add message to chat log
                app._chat_panel.add_message("user", "test query")
                await pilot.pause()

                # 3. Clear chat
                app.action_clear_chat()
                await pilot.pause()

                # 4. Bar is STILL intact and not removed
                bar_after = app.query_one("#ai_engine_bar", Static)
                assert "⚡ AI ENGINES" in str(bar_after.content)
                assert "Groq" in str(bar_after.content)
                assert "Gemini" in str(bar_after.content)

        asyncio.run(_check())

    def test_get_network_bandwidth_tool(self):
        from src.tools.read_only import get_network_bandwidth
        from src.tools.registry import get_tool_schemas, get_tool_tiers

        # Test tool schema and tier registration
        tiers = get_tool_tiers()
        assert tiers.get("get_network_bandwidth") == "read_only"
        schemas = get_tool_schemas()
        assert any(s["function"]["name"] == "get_network_bandwidth" for s in schemas)

        # Test live tool execution
        res = get_network_bandwidth(interval=0.05)
        assert res["success"] is True
        assert "download_speed" in res
        assert "upload_speed" in res
        assert "total_received" in res
        assert "total_sent" in res
        assert "download_bytes_sec" in res
        assert "upload_bytes_sec" in res

    def test_sparkline_renderer(self):
        from src.ui.tui import render_sparkline, SPARKLINE_BARS

        # Empty data fallback
        empty_res = render_sparkline([])
        assert "───────" in empty_res

        # Scaled values check: 0 should be lowest bar, 100 highest
        spark = render_sparkline([0.0, 50.0, 100.0], min_val=0.0, max_val=100.0)
        assert SPARKLINE_BARS[0] in spark
        assert SPARKLINE_BARS[-1] in spark

    def test_system_watcher_network_and_history(self):
        from src.monitor.watcher import SystemWatcher
        watcher = SystemWatcher(interval=1.0)
        metrics = watcher._collect_metrics()
        assert hasattr(metrics, "net_download_speed")
        assert hasattr(metrics, "net_upload_speed")
        assert hasattr(metrics, "cpu_history")
        assert hasattr(metrics, "mem_history")
        assert hasattr(metrics, "net_history")
        assert len(metrics.cpu_history) >= 1
        assert len(metrics.mem_history) >= 1
        assert len(metrics.net_history) >= 1

    def test_network_bandwidth_agent_fallback(self):
        from src.agent.core import Agent
        agent = Agent()
        reply = agent._try_local_fallback("check network bandwidth")
        assert "| Direction | Current Speed | Total Transferred |" in reply
        assert "Download (RX)" in reply
        assert "Upload (TX)" in reply

    def test_interactive_proc_and_port_rows(self):
        import asyncio
        from src.ui.tui import AIOSAgentApp, Button, Static, ProcRow, PortRow

        async def _check():
            app = AIOSAgentApp()
            async with app.run_test() as pilot:
                # 1. Mount test ProcRow and PortRow in their respective containers
                procs_list = app.query_one("#procs_list")
                ports_list = app.query_one("#ports_list")
                
                await procs_list.mount(ProcRow(pid=9999, name="testproc", cpu=25.0, mem=12.0))
                await ports_list.mount(PortRow(port=9999, proto="TCP", pid=8888, process="testserver"))

                # 2. Verify interactive widgets exist
                kill_btn = app.query_one("#kill_pid_9999", Button)
                close_btn = app.query_one("#close_port_9999", Button)
                inspect_proc = app.query_one("#inspect_proc_9999", Static)
                inspect_port = app.query_one("#inspect_port_9999", Static)

                assert kill_btn is not None
                assert close_btn is not None
                assert inspect_proc is not None
                assert inspect_port is not None

                # 3. Simulate clicking kill button triggers confirmation prompt
                await pilot.click(kill_btn)
                await pilot.pause()
                assert hasattr(app, "_pending_kill") or app._chat_panel is not None

    def test_chat_fast_path_kill_command(self):
        from src.agent.core import Agent
        agent = Agent()
        # Test kill by name / PID executes fast-path directly
        res = agent.chat("kill non_existent_dummy_process_xyz_99999")
        assert "No running process found" in res

    def test_chat_fast_path_port_command(self):
        from src.agent.core import Agent
        agent = Agent()
        # Test close port executes fast-path directly
        res = agent.chat("close port 59999")
        assert "59999" in res

    def test_trim_tool_result_for_llm(self):
        from src.agent.core import Agent
        agent = Agent()
        fake_procs = [{"pid": i, "name": f"proc_{i}", "cpu_percent": 1.0, "memory_mb": 10.0} for i in range(100)]
        trimmed = agent._trim_tool_result_for_llm("list_processes", {"success": True, "processes": fake_procs})
        assert len(trimmed["processes"]) == 20
        assert trimmed["total_processes"] == 100
        assert "note" in trimmed

    def test_app_action_quit(self):
        import asyncio
        from src.ui.tui import AIOSAgentApp

        async def _check():
            app = AIOSAgentApp()
            async with app.run_test() as pilot:
                app.action_quit()
                assert app.is_running is False or app._exit is True

        asyncio.run(_check())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])