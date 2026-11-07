"""Real Plotly click -> HTTP -> local fit -> disk -> fresh Workbench/page.

Run: python -m pytest tests/test_l1_review_browser.py --run-browser -q
Install the browser extra and run `python -m playwright install chromium`, or
set GRID_TEST_BROWSER_CHANNEL=chrome to use an installed Chrome headlessly.
No application functions, network responses, DOM or Plotly are replaced.
"""

import hashlib
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from calibration_lib.workbench.acquisition import default_profile
from calibration_lib.workbench.l1_review import overview
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench
from test_tb_first_fit import HISTOGRAM, l0, settled

pytestmark = pytest.mark.browser


class TestHost:
    __test__ = False

    def __init__(self, workspace):
        self.workspace, self.port = workspace, 0
        self.start()

    def start(self):
        self.service = Workbench(self.workspace)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self.service))
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()

    def restart(self):
        previous = self.service
        self.close()
        self.start()
        assert self.service is not previous


def point(service, run_id, stem, channel):
    return next(file for file in overview(service, run_id)["files"] if file["stem"] == stem)["channels"][channel]


def ready(page, point_id=None):
    page.wait_for_function(
        """id => !!state.run && !state.loading && !state.selecting && !state.busy &&
        !!state.spectrum && (!id || state.point?.id === id)""", arg=point_id,
    )


def snapshot(page):
    return page.evaluate("""() => ({point:state.point, spectrum:state.spectrum,
        lo:Number(document.getElementById('window-lo').value),
        hi:Number(document.getElementById('window-hi').value),
        traces:document.getElementById('surface-chart').data.map(({name,x,y})=>({name,x,y}))})""")


def select_spectrum(page, stem, channel, click):
    def selected(response):
        url = urlsplit(response.url)
        query = parse_qs(url.query)
        return url.path.endswith("/spectrum") and query.get("stem") == [stem] and query.get("channel") == [str(channel)]

    with page.expect_response(selected) as received:
        click()
    response = received.value
    assert response.status == 200
    query = parse_qs(urlsplit(response.url).query)
    assert query["layer"] == query["consumer_layer"] == ["TB_L1"]
    data = response.json()
    assert (data["id"], data["stem"], data["channel"]) == (f"10B:{stem}:{channel}", stem, channel)
    assert Path(data["source_path"]).name == f"{stem}.l0.parquet"
    assert len(data["x"]) == len(data["y"]) > 0
    assert sum(data["y"]) == data["n_events"] == 6800
    ready(page, data["id"])
    assert page.evaluate("state.spectrum.id") == data["id"]
    return data


def click_map(page, point_id):
    chart = page.locator("#l1-quality-overview")
    if not chart.evaluate("element => element.open"):
        chart.locator("summary").first.click()
    page.wait_for_function("document.getElementById('l1-quality-chart').data?.length > 0")
    page.locator("#l1-quality-chart").scroll_into_view_if_needed()
    position = page.locator("#l1-quality-chart").evaluate("""(chart, id) => {
        const trace=chart.data.find(t=>t.customdata?.some(p=>p.id===id));
        if (!trace) throw new Error('Missing plotted identity '+id);
        const i=trace.customdata.findIndex(p=>p.id===id), r=chart.getBoundingClientRect();
        const {xaxis,yaxis}=chart._fullLayout;
        return {x:r.left+xaxis._offset+xaxis.l2p(trace.x[i]),
                y:r.top+yaxis._offset+yaxis.l2p(trace.y[i])};
    }""", point_id)
    page.mouse.click(position["x"], position["y"])


def fit_window(page, lo, hi, expected_status):
    page.locator("#window-lo").fill(str(lo))
    page.locator("#window-hi").fill(str(hi))
    with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path.endswith("/jobs")) as sent:
        page.locator("#fit-spectrum").click()
    response = sent.value
    assert response.status == 202
    request = response.request.post_data_json
    assert request["stem"] == "case" and request["channel"] == 2
    assert request["fit_channels"] == [2] and request["automatic_window"] is False
    page.wait_for_function("""expected => !state.busy && !state.selecting &&
        state.run.jobs.at(-1)?.status === expected""", arg=expected_status)
    ready(page, "10B:case:2")
    return page.evaluate("state.run.jobs.at(-1)")


def test_quality_selection_refit_save_and_reopen(tmp_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.fail("Install the browser extra to use --run-browser: pip install -e .[dev,workbench,browser]")

    workspace = tmp_path / "workspace"
    history = workspace / "products"
    centers = [2000, 2100, 2200, 2300]
    l0(history, centers=centers)
    l0(history, "case-extra", centers=[3000, 3100, 3200, 3300], hk_data={})
    host = TestHost(workspace)
    try:
        dataset = host.service.scan_datasets(product_dir=str(history / "10B"))["datasets"][0]
        run = host.service.create(dataset_id=dataset["id"])
        run_id = run["id"]
        host.service.save_acquisition(run_id, default_profile("10B"))
        sha = host.service.read(run_id)["acquisition"]["sha256"]
        l0(history, centers=centers, profile_sha=sha)
        l0(history, "case-extra", centers=[3000, 3100, 3200, 3300], hk_data={}, profile_sha=sha)
        source_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in history.rglob("*.parquet")}
        # Unselected channels already have actual processor results to preserve.
        host.service.patch(run_id, {"windows": {"case": {
            "0": {"lo": 1700, "hi": 2310}, "1": {"lo": 1800, "hi": 2410},
        }}})
        seeded = settled(host.service, run_id, host.service.submit(run_id, {
            "layer": "TB_L1", "stem": "case", "fit_channels": [0, 1], "representative": False,
        }))
        assert seeded["status"] == "succeeded", seeded.get("error")
        unselected = {ch: host.service.spectrum(run_id, "case", ch) for ch in (0, 1, 3)}
        assert all(unselected[ch]["curve"] for ch in (0, 1))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel=os.getenv("GRID_TEST_BROWSER_CHANNEL") or None)
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            errors, requests = [], []

            def new_page(*, restoring=False):
                page = context.new_page()
                page.set_default_timeout(30_000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append({
                    "method": request.method, "path": urlsplit(request.url).path,
                    "query": parse_qs(urlsplit(request.url).query),
                        "body": request.post_data_json if request.method in ("POST", "PATCH") else None,
                }) if "/api/" in request.url else None)
                if restoring:
                    select_spectrum(page, "case", 2, lambda: page.goto(host.url))
                else:
                    page.goto(host.url)
                    ready(page)
                return page

            page = new_page()
            page.locator("button[data-layer='TB_L1']").click()
            ready(page)
            page.locator("#channel").select_option("3")
            ready(page, "10B:case:3")
            count = len(host.service.read(run_id)["jobs"])
            pending = select_spectrum(page, "case", 3, lambda: click_map(page, "10B:case:3"))
            assert pending["fit"] is pending["curve"] is None
            assert pending["unsupported_spectral_model"]
            assert page.locator("#fit-spectrum").is_disabled()
            assert "模型" in page.locator("#window-status").inner_text()
            assert page.locator("#stage-status").inner_text() == "拟合方案待定"
            assert point(host.service, run_id, "case", 3)["fit_status"] == "model_pending"
            assert len(host.service.read(run_id)["jobs"]) == count

            page.locator("#channel").select_option("2")
            ready(page, "10B:case:2")
            page.locator("#l1-quality-unlocated > summary").click()
            missing = select_spectrum(page, "case-extra", 2, lambda: page.locator(
                "#l1-quality-unlocated-list button", has_text="case-extra",
            ).click())
            assert missing["fit"] is missing["curve"] is None
            missing_point = point(host.service, run_id, "case-extra", 2)
            assert missing_point["temp"] is missing_point["bias"] is None
            assert not page.evaluate("""document.getElementById('l1-quality-chart').data
                .some(t=>t.customdata?.some(p=>p.stem==='case-extra'))""")
            assert missing["y"] != pending["y"]
            select_spectrum(page, "case", 2, lambda: click_map(page, "10B:case:2"))
            assert snapshot(page)["spectrum"]["y"] != unselected[0]["y"]

            failed = fit_window(page, 12000, 13000, "failed")
            assert point(host.service, run_id, "case", 2)["fit_status"] == "failed"
            failed_spectrum = select_spectrum(page, "case", 2, lambda: click_map(page, "10B:case:2"))
            assert failed_spectrum["last_fit_attempt"]["id"] == failed["id"]
            assert failed_spectrum["last_fit_attempt"]["status"] == "failed"
            assert failed_spectrum["curve"] is None
            assert (snapshot(page)["lo"], snapshot(page)["hi"]) == (12000, 13000)
            assert "上次拟合" in page.locator("#spectrum-params").inner_text()

            succeeded = fit_window(page, 1900.125, 2510.875, "succeeded")
            assert host.service.job(run_id, succeeded["id"])["result"]["processor_class"] == "TBL1Processor"
            with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/config")):
                page.locator("#save-run").click()
            ready(page)
            fitted = snapshot(page)
            assert abs(fitted["spectrum"]["fit"]["center"] - centers[2]) < 20
            assert (fitted["lo"], fitted["hi"]) == (1900.125, 2510.875)
            assert fitted["spectrum"]["histogram_config"] == HISTOGRAM
            rendered_fit = next(t for t in fitted["traces"] if t["name"] == "峰 + 背景拟合")
            assert rendered_fit["x"] == fitted["spectrum"]["curve"]["x"]
            np.testing.assert_allclose(rendered_fit["y"], fitted["spectrum"]["curve"]["y"], rtol=1e-12)
            assert fitted["point"]["fit_status"] == "fitted"
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(tmp_path / "fitted.png"), full_page=True)
            for ch, old in unselected.items():
                assert host.service.spectrum(run_id, "case", ch) == old
            assert len(host.service.read(run_id)["jobs"]) == count + 2

            def reopen(expected):
                jobs = len(host.service.read(run_id)["jobs"])
                old_page = page
                old_page.close()
                host.restart()
                fresh = new_page(restoring=True)
                ready(fresh, "10B:case:2")
                actual = snapshot(fresh)
                assert actual["point"]["id"] == expected["point"]["id"] == "10B:case:2"
                assert (actual["lo"], actual["hi"]) == (expected["lo"], expected["hi"])
                assert actual["spectrum"]["fit"] == expected["spectrum"]["fit"]
                assert actual["spectrum"]["last_fit_attempt"] == expected["spectrum"]["last_fit_attempt"]
                assert actual["spectrum"]["curve"] == expected["spectrum"]["curve"]
                actual_curve = next(t for t in actual["traces"] if t["name"] == "峰 + 背景拟合")
                expected_curve = next(t for t in expected["traces"] if t["name"] == "峰 + 背景拟合")
                np.testing.assert_allclose(actual_curve["y"], expected_curve["y"], rtol=1e-12)
                assert len(host.service.read(run_id)["jobs"]) == jobs
                return fresh

            page = reopen(fitted)
            assert point(host.service, run_id, "case", 2)["fit_status"] == fitted["point"]["fit_status"]
            # Failed latest attempt must remain visible alongside the prior curve.
            failed_again = fit_window(page, 12000, 13000, "failed")
            retained = snapshot(page)
            assert retained["spectrum"]["curve"] == fitted["spectrum"]["curve"]
            assert retained["spectrum"]["last_fit_attempt"]["id"] == failed_again["id"]
            assert retained["point"]["fit_status"] == "failed"
            page = reopen(retained)
            assert page.evaluate("state.point.fit_status") == "failed"
            assert "计算失败" in page.locator("#stage-status").inner_text()
            assert "上次拟合" in page.locator("#spectrum-params").inner_text()
            assert any(t["name"] == "峰 + 背景拟合" for t in snapshot(page)["traces"])
            assert point(host.service, run_id, "case", 3)["fit_status"] == "model_pending"
            assert point(host.service, run_id, "case-extra", 2)["temp"] is None
            for ch, old in unselected.items():
                assert host.service.spectrum(run_id, "case", ch) == old
                response = select_spectrum(page, "case", ch, lambda ch=ch: page.locator("#channel").select_option(str(ch)))
                assert response == old
                assert "失败" not in page.locator("#stage-status").inner_text()
            select_spectrum(page, "case", 2, lambda: page.locator("#channel").select_option("2"))
            assert "计算失败" in page.locator("#stage-status").inner_text()
            assert len(host.service.read(run_id)["jobs"]) == count + 3
            assert source_hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_hashes}
            assert not errors, errors
            submitted = [r["body"] for r in requests if r["method"] == "POST" and r["path"].endswith("/jobs")]
            assert len(submitted) == 3
            assert all(spec["stem"] == "case" and spec["fit_channels"] == [2] for spec in submitted)
            (tmp_path / "requests.json").write_text(json.dumps(requests, indent=2), encoding="utf-8")
            (tmp_path / "acceptance.json").write_text(json.dumps({
                "run_id": run_id, "workspace": str(workspace), "point_id": retained["point"]["id"],
                "first_failure": failed["id"], "success": succeeded["id"], "latest_failure": failed_again["id"],
                "curve_samples": len(fitted["spectrum"]["curve"]["x"]), "unselected_channels": [0, 1, 3],
                "saved_window": [fitted["lo"], fitted["hi"]], "latest_window": [retained["lo"], retained["hi"]],
                "fitted_center": fitted["spectrum"]["fit"]["center"], "latest_status": retained["point"]["fit_status"],
                "browser_errors": errors, "workbench_rebuilds": 2,
            }, indent=2), encoding="utf-8")
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(tmp_path / "reopened.png"), full_page=True)
            context.close()
            browser.close()
    finally:
        host.close()
