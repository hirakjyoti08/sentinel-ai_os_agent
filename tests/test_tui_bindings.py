"""Keyboard shortcuts must work while the chat input (which has focus) is active."""

from textual.binding import Binding
from textual.widgets import Input

from sentinel.ui.tui import AIOSAgentApp


def test_ctrl_u_undo_wins_over_input_delete_line():
    # Input binds ctrl+u to "delete_left_all"; without priority the app's undo never fires.
    assert any("ctrl+u" in b.key for b in Input.BINDINGS)
    undo = [b for b in AIOSAgentApp.BINDINGS if isinstance(b, Binding) and b.action == "undo"]
    assert undo and undo[0].key == "ctrl+u" and undo[0].priority
