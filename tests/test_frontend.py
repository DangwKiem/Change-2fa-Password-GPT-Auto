r"""Browser regression tests; all HTTP/SSE data is local and synthetic.

Run: .venv\Scripts\python.exe -m unittest discover -s tests -v
Requires Playwright and a local Chrome/Edge install (or PLAYWRIGHT_CHROMIUM_EXECUTABLE).
"""
import json
import os
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {
    "twofa.max_concurrent": 3, "twofa.job_timeout": 180,
    "twofa.auto_retry": False, "twofa.auto_retry_max": 1,
    "twofa.auto_retry_delay": 3, "twofa.change_enabled": False,
    "twofa.input_draft": "",
}


def job(identifier, **changes):
    result = dict(
        id=identifier, email=f"{identifier}@example.test", mode="change_2fa",
        status="running", error=None, error_kind=None, account_state="unknown",
        plan=None, plan_source=None, plan_expires=None, is_trial=None,
        usage_summary=None, usage_info=None, retryable=True,
        rotated_pending_verify=False, password_changed=False, login_verified=False,
        retry_count=0, has_new_password=False, created_at=1, started_at=2,
        finished_at=None, log_tail=[], logs=[],
    )
    result.update(changes)
    return result


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        candidates = [os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""),
                      "C:/Program Files/Google/Chrome/Application/chrome.exe",
                      "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]
        executable = next((path for path in candidates if Path(path).is_file()), None)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.page = self.context.new_page()
        self.calls = []
        self.pending = []
        self.hold = set()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.route)
        self.page.add_init_script("""
            window.EventSource = class {
                constructor() { window.__events = this; }
                close() {}
            };
            window.emit = payload => window.__events.onmessage({data: JSON.stringify(payload)});
        """)
        self.page.goto("http://dashboard.test/")
        self.page.wait_for_function("window.__events && document.querySelectorAll('#job-list tr').length === 2")
        self.page.wait_for_timeout(100)

    def tearDown(self):
        for _, route in self.pending:
            route.abort()
        self.context.unroute_all(behavior="ignoreErrors")
        self.context.close()

    def route(self, route):
        request = route.request
        path = request.url.split("dashboard.test")[-1].split("?")[0]
        if not request.url.startswith("http://dashboard.test/"):
            return route.abort()
        if path.startswith("/api/"):
            self.calls.append((path, request.post_data_json if request.post_data else None))
            if path in self.hold:
                self.pending.append((path, route))
                return
            if path == "/api/bootstrap":
                body = {"token": "test-token", "settings": SETTINGS,
                        "jobs": [job("a"), job("b")]}
            elif path == "/api/output":
                return route.fulfill(status=200, content_type="text/plain", body="")
            elif path == "/api/settings":
                body = {"settings": SETTINGS}
            elif path.endswith("/logs"):
                body = {"logs": ["initial log"]}
            else:
                body = {"ok": True, "jobs": []}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        file = ROOT / ("static/index.html" if path == "/" else "static/" + path.removeprefix("/assets/"))
        content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
        route.fulfill(status=200, content_type=content_type, body=file.read_bytes())

    def emit(self, payload):
        self.page.evaluate("payload => window.emit(payload)", payload)

    def count_calls(self, path):
        return sum(item[0] == path for item in self.calls)

    def test_log_burst_keeps_row_nodes_and_does_not_refetch_output(self):
        """Replacing tbody or fetching output for log-only events breaks this."""
        self.page.evaluate("""() => {
            window.rows = [...document.querySelectorAll('#job-list tr')];
            window.rowMutations = 0;
            new MutationObserver(records => window.rowMutations += records.length)
                .observe(document.querySelector('#job-list'), {childList:true, subtree:true});
        }""")
        before = self.count_calls("/api/output")
        self.page.evaluate("""sample => {
            for (let i=0; i<100; i++) window.emit({type:'job', job:{...sample, logs:['line '+i]}});
        }""", job("a"))
        self.page.wait_for_timeout(200)
        self.assertTrue(self.page.evaluate("window.rows.every(row => row.isConnected)"))
        self.assertEqual(self.page.evaluate("window.rowMutations"), 0)
        self.assertEqual(self.count_calls("/api/output"), before)

    def test_status_update_preserves_other_row_and_filter_counts(self):
        self.page.evaluate("window.otherRow = document.querySelector('tr[data-id=b]')")
        self.emit({"type": "job", "job": job("a", status="error", error="Network issue")})
        self.page.wait_for_function("document.querySelector('#count-error').textContent === '1'")
        self.assertTrue(self.page.evaluate("window.otherRow === document.querySelector('tr[data-id=b]')"))
        self.page.click("#filter-error")
        self.assertEqual(self.page.locator("#job-list tr").count(), 1)
        self.assertIn("a@example.test", self.page.locator("#job-list").inner_text())

    def test_large_queue_log_updates_do_not_mutate_table_or_counters(self):
        self.emit({"type": "snapshot", "jobs": [job(str(i), created_at=i) for i in range(500)]})
        self.page.wait_for_function("document.querySelectorAll('#job-list tr').length === 500")
        self.page.evaluate("""() => {
            window.queueMutations = 0;
            const observer = new MutationObserver(records => window.queueMutations += records.length);
            observer.observe(document.querySelector('#job-list'), {childList:true, attributes:true, subtree:true});
            observer.observe(document.querySelector('#count-running'), {childList:true, subtree:true});
        }""")
        for i in range(5):
            self.emit({"type": "job", "job": job("0", created_at=0, logs=[f"line {i}"])})
            self.page.wait_for_timeout(25)
        self.assertEqual(self.page.evaluate("window.queueMutations"), 0)
        self.assertEqual(self.page.locator("#count-running").inner_text(), "500")

    def test_reverting_concurrency_while_save_pending_is_not_lost(self):
        self.hold.add("/api/settings")
        self.page.fill("#quick-concurrency", "5")
        self.page.locator("#quick-concurrency").dispatch_event("change")
        self.page.wait_for_timeout(60)
        self.page.fill("#quick-concurrency", "3")
        self.page.locator("#quick-concurrency").dispatch_event("change")
        self.page.wait_for_timeout(60)
        _, route = self.pending.pop(0)
        route.fulfill(status=200, json={"settings": {**SETTINGS, "twofa.max_concurrent": 5}})
        self.page.wait_for_timeout(100)
        self.assertEqual(self.calls[-1][1]["max_concurrent"], 3)

    def test_success_burst_coalesces_output_requests(self):
        before = self.count_calls("/api/output")
        self.page.evaluate("""samples => {
            for (const sample of samples) window.emit({type:'job', job:sample});
        }""", [job("a", status="success", login_verified=True),
                job("b", status="success", login_verified=True)])
        self.page.wait_for_timeout(400)
        self.assertEqual(self.count_calls("/api/output") - before, 1)

    def test_output_invalidated_during_request_is_refreshed_afterwards(self):
        self.hold.add("/api/output")
        self.emit({"type": "job", "job": job("a", status="success", login_verified=True)})
        self.page.wait_for_timeout(400)
        self.assertEqual(len(self.pending), 1)
        self.emit({"type": "removed", "id": "a"})
        self.page.wait_for_timeout(200)
        self.assertEqual(len(self.pending), 1, "Do not overlap output requests")
        self.hold.remove("/api/output")
        self.pending.pop()[1].fulfill(status=200, content_type="text/plain", body="old-result")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.page.locator("#success-output").input_value(), "")

    def test_late_log_response_does_not_replace_new_selection(self):
        self.hold.add("/api/jobs/a/logs")
        self.page.click("tr[data-id=a]")
        self.page.wait_for_timeout(60)
        self.page.click("tr[data-id=b]")
        self.page.wait_for_function("document.querySelector('#inline-log-content').textContent === 'initial log'")
        self.pending.pop()[1].fulfill(status=200, json={"logs": ["STALE A LOG"]})
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator("#inline-log-content").inner_text(), "initial log")

    def test_live_log_does_not_force_scroll_while_reading_history(self):
        self.page.click("tr[data-id=a]")
        self.page.wait_for_timeout(60)
        lines = [f"line {index}" for index in range(300)]
        self.emit({"type": "job", "job": job("a", logs=lines)})
        self.page.wait_for_timeout(60)
        self.page.locator("#inline-log-content").evaluate("node => node.scrollTop = 0")
        self.emit({"type": "job", "job": job("a", logs=lines + ["latest"])})
        self.page.wait_for_timeout(60)
        self.assertEqual(self.page.locator("#inline-log-content").evaluate("node => node.scrollTop"), 0)

    def test_settings_writes_wait_and_keep_new_concurrency(self):
        self.hold.add("/api/settings")
        self.page.fill("#combo-input", "draft@example.test|password|SECRET")
        self.page.wait_for_timeout(550)
        self.assertEqual(len(self.pending), 1)
        self.page.fill("#quick-concurrency", "5")
        self.page.locator("#quick-concurrency").dispatch_event("change")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.count_calls("/api/settings"), 1, "Serialize writes to full-settings endpoint")
        _, route = self.pending.pop(0)
        route.fulfill(status=200, json={"settings": SETTINGS})
        self.page.wait_for_timeout(100)
        self.assertEqual(self.calls[-1][1]["max_concurrent"], 5)
        _, route = self.pending.pop(0)
        route.fulfill(status=200, json={"settings": {**SETTINGS, "twofa.max_concurrent": 5}})
        self.page.wait_for_timeout(60)
        self.assertEqual(self.page.locator("#quick-concurrency").input_value(), "5")

    def test_stop_all_is_single_flight(self):
        self.hold.add("/api/jobs/stop-all")
        self.page.evaluate("""() => {
            document.querySelector('#stop-all').click();
            document.querySelector('#stop-all').click();
        }""")
        self.page.wait_for_timeout(80)
        self.assertEqual(self.count_calls("/api/jobs/stop-all"), 1)
        self.assertTrue(self.page.locator("#stop-all").is_disabled())
        self.pending.pop()[1].fulfill(status=200, json={"ok": True})
        self.page.wait_for_timeout(60)
        self.assertFalse(self.page.locator("#stop-all").is_disabled())

    def test_opening_log_does_not_shrink_table(self):
        before = self.page.locator(".table-wrap").bounding_box()["width"]
        self.page.click("tr[data-id=a]")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.locator(".table-wrap").bounding_box()["width"], before)

    def test_mobile_controls_and_reduced_motion(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.emulate_media(reduced_motion="reduce")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.evaluate("getComputedStyle(document.documentElement).scrollBehavior"), "auto")
        self.assertLessEqual(self.page.locator("#launch-batch").bounding_box()["x"] +
                             self.page.locator("#launch-batch").bounding_box()["width"], 390)
        self.assertFalse(self.errors)

    def test_mode_labels_fit_on_desktop_and_mobile(self):
        for width in [1440, 390]:
            self.page.set_viewport_size({"width": width, "height": 900})
            self.page.wait_for_timeout(60)
            self.assertTrue(self.page.locator(".mode-label").evaluate_all(
                "nodes => nodes.every(node => node.scrollWidth <= node.clientWidth)"),
                f"Mode names must not be truncated at width {width}")

    def test_dashboard_does_not_wait_for_external_font_stylesheets(self):
        self.assertEqual(self.page.locator('link[rel="stylesheet"][href^="https://"]').count(), 0)
        self.assertIn("sans-serif", self.page.locator(".section-head h2").first.evaluate(
            "node => getComputedStyle(node).fontFamily"))


if __name__ == "__main__":
    unittest.main()
