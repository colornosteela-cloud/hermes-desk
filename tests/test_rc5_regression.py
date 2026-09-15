from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RC5RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (ROOT / "ui" / "index.html").read_text()
        cls.css = (ROOT / "ui" / "hermesbot.css").read_text()
        cls.app = (ROOT / "ui" / "app.js").read_text()
        cls.ui = (ROOT / "ui" / "hermesbot-ui.js").read_text()
        cls.mcp = (ROOT / "deskd" / "desktop_mcp.py").read_text()
        cls.deskd = (ROOT / "deskd" / "deskd.py").read_text()

    def test_rc3_workspace_controls_still_exist_for_behavior_compatibility(self):
        # These controls remain in the DOM so the previous frontend wiring is not removed.
        self.assertIn('class="workspace-header"', self.index)
        self.assertIn('class="desktop-shortcuts"', self.index)
        self.assertIn('id="hourly-modal"', self.index)
        self.assertIn('id="desktop-fullscreen"', self.index)
        self.assertIn('id="takeover"', self.index)
        self.assertIn('id="workspace-collapse"', self.index)
        self.assertIn('id="live-desktop-exit"', self.index)
        self.assertIn('id="screen-open"', self.index)
        self.assertIn('id="screen-open" class="icon-btn" type="button" title="Show workspace">‹</button>', self.index)

    def test_cosmetic_elements_are_hidden_not_deleted(self):
        self.assertIn('.desktop-shortcuts { display:none !important; }', self.css)
        self.assertIn('.notes-section { display:none !important; }', self.css)
        self.assertIn('.workspace-header { display:none !important; }', self.css)
        self.assertIn('#reopen-left { display:none !important; }', self.css)
        self.assertIn('id="reopen-left"', self.index)
        self.assertIn('id="nav-toggle"', self.index)
        self.assertIn('id="nav-toggle" class="icon-btn header-toggle" type="button" title="Show bots">›</button>', self.index)
        self.assertIn('id="routines-open"', self.index)
        self.assertIn('class="workspace-footer"', self.index)

    def test_routines_open_add_form_in_one_window(self):
        self.assertLess(self.index.find('id="routine-modal"'), self.index.find('id="routine-list"'))
        self.assertIn('class="routine-modal-existing"', self.index)
        self.assertIn("Current routines", self.index)
        self.assertIn('$("routines-open")?.addEventListener("click", () => openRoutineModal())', self.ui)

    def test_routines_are_generalized(self):
        for unit in ("minutes", "hours", "days", "weeks", "monthly", "yearly"):
            self.assertIn(f'value="{unit}"', self.index)
        self.assertIn('id="routine-every"', self.index)
        self.assertIn('id="routine-unit"', self.index)

    def test_semantic_desktop_driver_is_backend_additive(self):
        for name in (
            "desktop_state", "desktop_open_app", "desktop_focus_window",
            "desktop_minimize_window", "desktop_maximize_window", "desktop_close_window",
            "desktop_click_object", "desktop_open_file", "desktop_open_preview",
            "desktop_browser_navigate", "desktop_browser_back", "desktop_browser_forward",
            "desktop_run_tests", "desktop_run_app", "desktop_stop_app",
        ):
            self.assertIn(name, self.mcp)
        self.assertIn("screen_objects", self.deskd)
        self.assertIn("recommended_next_actions", self.deskd)
        self.assertNotIn("Never inspect Hermes Desk source code", self.deskd)

    def test_semantic_actions_are_cherry_picked_into_existing_rc3_frontend(self):
        self.assertIn("function applyDesktopAction", self.app)
        self.assertIn("semanticWindowAction", self.app)
        self.assertIn("window.DeskUI?.openWindow", self.app)
        self.assertIn("focusWindow: focusDesktopWindow", self.ui)
        self.assertIn("minimizeWindow: minimizeDesktopWindow", self.ui)
        self.assertIn("maximizeWindow: toggleMaximizeDesktopWindow", self.ui)
        self.assertIn("closeWindow: closeDesktopWindow", self.ui)


if __name__ == "__main__":
    unittest.main()
