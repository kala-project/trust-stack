"""Controlled engineering fixture for long explicit-matrix reader pagination.

This is synthetic test data, not a real research report, visual approval, or
independent user acceptance.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import json
import os
import tempfile
import threading
import unittest

from scripts.browser_smoke import BrowserSmoke


CHROME = os.environ.get("CHROMIUM_BINARY") or "/snap/chromium/current/usr/lib/chromium-browser/chrome"
WEB = Path(__file__).resolve().parents[1] / "web"


def fixture_payload():
    presentation_rows = []
    points = []
    for row_index in range(25):
        cells = []
        for column_index in range(2):
            point_id = f"point-{row_index:02d}-{column_index}"
            cells.append({"point_id": point_id, "anchor_id": f"{point_id}-cell-value"})
            points.append({
                "id": point_id,
                "label": f"不会用于绑定的标签 {49 - (row_index * 2 + column_index)}",
                "value": str(1000 + row_index * 10 + column_index),
                "unit": "受控单位",
                "display_text": f"{1000 + row_index * 10 + column_index} 受控单位",
                "basis": "仅用于工程分页验证",
                "period": {"kind": "year", "label": str(2025 + column_index)},
                "anchor_id": f"{point_id}-cell-value",
            })
        presentation_rows.append({
            "key": f"metric-{row_index:02d}",
            "label": (f"受控指标 {row_index + 1}；这一行包含不同长度的说明文字，"
                      + ("用于触发真实 DOM 自动换行。" * (1 + row_index % 3))),
            "cells": cells,
        })
    # Deliberately make saved point order differ from presentation order.
    points = points[::2][::-1] + points[1::2]
    description_anchor = {
        "id": "matrix-description-anchor", "kind": "claim",
        "text": "合成 fixture 的表说明核查入口", "reference_keys": [],
    }
    dataset = {
        "id": "matrix-50-points", "type": "dataset", "title": "五十点受控长矩阵",
        "description": "合成 fixture：验证完整行分页，不代表真实研究结论。",
        "description_anchor_id": description_anchor["id"],
        "rows": points,
        "presentation": {
            "type": "matrix", "after_block_id": "intro", "unit": "受控单位",
            "note": "说明：本表仅供自动化工程验证；不是完整真实报告或独立用户验收。",
            "columns": [{"key": "c0", "label": "期间甲"}, {"key": "c1", "label": "期间乙"}],
            "rows": presentation_rows,
        },
    }
    return {
        "schema_version": "report-reader-payload/1",
        "report": {"id": "controlled-long-matrix", "conversation_id": "fixture-only", "title": "受控长矩阵分页工程测试", "status_label": "合成 fixture"},
        "question": {"original": "验证长矩阵分页", "scope": {}},
        "sources": [], "anchors": [description_anchor], "audit": {"semantic_review": {"status": "not_assessed"}},
        "chapters": [{"id": "analysis", "title": "工程验证", "blocks": [
            {"id": "question", "type": "question", "text": "验证长矩阵是否沿完整行按真实纸宽与 DOM 高度分页。"},
            {"id": "intro", "type": "finding", "title": "fixture 声明", "text": "这是合成工程 fixture，不是真实报告，也不构成视觉或独立用户验收。"},
            dataset,
        ]}],
    }


class FixtureHandler(SimpleHTTPRequestHandler):
    payload = fixture_payload()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/reports/reader?"):
            return self._json(self.payload)
        if self.path.startswith("/api/reports?"):
            report = self.payload["report"]
            return self._json({"reports": [{**report, "report_id": report["id"], "version": 1,
                                             "corrected_by": [], "corrections": []}]})
        if self.path.startswith("/api/reports/evidence?"):
            anchor = self.payload["anchors"][0]
            return self._json({
                "anchor": anchor,
                "evidence": {
                    "claim": anchor["text"],
                    "verification": {
                        "method": "受控浏览器回归仅检查入口与焦点。",
                        "boundary": "不代表真实报告或独立用户验收。",
                    },
                    "traces": [],
                },
                "frozen_detail": {"traces": []},
            })
        return super().do_GET()

    def _json(self, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(Path(CHROME).is_file(), "Controlled Chromium binary is unavailable")
class ReportReaderLongMatrixBrowserTests(unittest.TestCase):
    def test_complete_rows_fill_real_pages_at_three_widths(self):
        before = hashlib.sha256(json.dumps(FixtureHandler.payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FixtureHandler, directory=str(WEB)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = None
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
                root = Path(temporary)
                url = f"http://127.0.0.1:{server.server_port}/report-reader.html?report_id=controlled-long-matrix"
                browser = BrowserSmoke(url, CHROME, root / "browser-output", 60)
                browser.start(str(root / "profile"))
                browser.wait("document.querySelectorAll('[data-block-id=\"matrix-50-points\"] tbody tr').length === 25", "all matrix rows render")

                checks = []
                for width, height, mode in [(1440, 1000, "single"), (1600, 1000, "two"), (390, 844, "two")]:
                    browser.cdp.call("Emulation.setDeviceMetricsOverride", {
                        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 500,
                    })
                    browser.evaluate(f"document.querySelector('[data-reading-mode=\"{mode}\"]').click(); new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                    result = browser.evaluate(r"""(() => {
                      const blocks = [...document.querySelectorAll('[data-block-id="matrix-50-points"]')];
                      const buttons = [...document.querySelectorAll('[data-block-id="matrix-50-points"] [data-anchor-id]')];
                      const pointIds = buttons.map(node => node.dataset.anchorId).filter(id => id.startsWith('point-'));
                      const descriptionButtons = buttons.filter(node => node.dataset.anchorId === 'matrix-description-anchor');
                      const descriptions = blocks.map(block => block.querySelector('.reader-dataset-description'));
                      const rows = blocks.flatMap(block => [...block.querySelectorAll('tbody tr')]);
                      const captions = blocks.map(block => block.querySelector('figcaption').textContent);
                      const headers = blocks.map(block => block.querySelectorAll('thead').length);
                      const rowRanges = blocks.map(block => [Number(block.dataset.matrixRowStart || 0), Number(block.dataset.matrixRowEnd || 25)]);
                      const pages = [...document.querySelectorAll('.reader-page')];
                      const matrixPages = [...new Set(blocks.map(block => block.closest('.reader-page')))];
                      const noSpareCompleteRow = blocks.slice(0, -1).every((block, index) => {
                        const page = block.closest('.reader-page');
                        const nextRow = blocks[index + 1].querySelector('tbody tr');
                        const clone = page.cloneNode(true);
                        clone.hidden = false;
                        clone.querySelector('.reader-page-number')?.remove();
                        clone.querySelector('[data-block-id="matrix-50-points"] tbody').append(nextRow.cloneNode(true));
                        Object.assign(clone.style, {position:'absolute', visibility:'hidden', width:`${page.getBoundingClientRect().width}px`, height:'auto', minHeight:'0'});
                        document.body.append(clone);
                        const tooTall = clone.getBoundingClientRect().height > Number(page.dataset.layoutTarget) + 1;
                        clone.remove();
                        return tooTall;
                      });
                      return {
                        width: innerWidth, blocks: blocks.length, rows: rows.length, points: pointIds.length,
                        uniquePoints: new Set(pointIds).size, captions, headers, rowRanges, noSpareCompleteRow,
                        descriptions: descriptions.map(node => node?.childNodes[0]?.textContent),
                        descriptionTriggers: descriptionButtons.length,
                        descriptionTriggerBlock: blocks.indexOf(descriptionButtons[0]?.closest('[data-block-id="matrix-50-points"]')),
                        modeTwo: document.body.classList.contains('reading-two-page'),
                        pageWidths: pages.filter(page => !page.hidden).map(page => page.getBoundingClientRect().width),
                        pageHeights: pages.filter(page => !page.hidden).map(page => page.getBoundingClientRect().height),
                        footersInside: matrixPages.every(page => {
                          const footer=page.querySelector('.reader-page-number').getBoundingClientRect(), box=page.getBoundingClientRect();
                          return footer.bottom <= box.bottom && footer.top >= box.top;
                        }),
                        tocPage: Number(document.querySelector('a[href="#block-matrix-50-points"]')?.dataset.page),
                        firstMatrixPage: pages.indexOf(blocks[0].closest('.reader-page')),
                      };
                    })()""")
                    checks.append(result)
                    self.assertEqual((result["rows"], result["points"], result["uniquePoints"]), (25, 50, 50))
                    self.assertGreater(result["blocks"], 1)
                    self.assertEqual(len(result["descriptions"]), result["blocks"])
                    self.assertEqual(len(set(result["descriptions"])), 1, "description may repeat on continuation pages")
                    self.assertEqual(result["descriptionTriggers"], 1)
                    self.assertEqual(result["descriptionTriggerBlock"], 0)
                    self.assertTrue(all(value == 1 for value in result["headers"]))
                    self.assertNotIn("continued", result["captions"][0])
                    self.assertTrue(all("五十点受控长矩阵 (continued)" in value for value in result["captions"][1:]))
                    self.assertEqual(result["rowRanges"][0][0], 0)
                    self.assertEqual(result["rowRanges"][-1][1], 25)
                    self.assertTrue(all(left[1] == right[0] for left, right in zip(result["rowRanges"], result["rowRanges"][1:])))
                    self.assertTrue(result["noSpareCompleteRow"])
                    self.assertTrue(result["footersInside"])
                    self.assertEqual(result["tocPage"], result["firstMatrixPage"])
                self.assertTrue(checks[1]["modeTwo"])
                self.assertEqual(len(checks[1]["pageWidths"]), 2)
                self.assertLess(abs(checks[1]["pageWidths"][0] - checks[1]["pageWidths"][1]), 1)
                self.assertLess(abs(checks[1]["pageHeights"][0] - checks[1]["pageHeights"][1]), 1)
                self.assertFalse(checks[2]["modeTwo"], "mobile must fall back to one paper")

                description_selector = '[data-anchor-id="matrix-description-anchor"]'
                browser.evaluate(f"document.querySelector('{description_selector}').focus()")
                focused_id = browser.evaluate("document.activeElement.dataset.anchorId")
                browser.cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1180, "height": 900, "deviceScaleFactor": 1, "mobile": False})
                browser.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                self.assertEqual(browser.evaluate("document.activeElement.dataset.anchorId"), focused_id)
                self.assertEqual(browser.evaluate(f"document.querySelectorAll('[data-anchor-id=\"{focused_id}\"]').length"), 1)

                browser.evaluate(f"document.querySelector('{description_selector}').click()")
                browser.wait("document.querySelector('#evidence-title').textContent === 'Point-by-point review'", "description evidence opens")
                self.assertEqual(browser.evaluate("document.querySelector('.selected-claim p').textContent"),
                                 "合成 fixture 的表说明核查入口")
                browser.evaluate("document.querySelector('#evidence-close').click()")
                self.assertTrue(browser.evaluate("document.querySelector('#evidence-panel').hidden"))
                self.assertEqual(browser.evaluate("document.activeElement.dataset.anchorId"), focused_id)
                self.assertTrue(browser.evaluate("!document.activeElement.closest('.reader-page').hidden"))
        finally:
            if browser:
                browser.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        after = hashlib.sha256(json.dumps(FixtureHandler.payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(after, before, "reader projection must not mutate the controlled payload")


if __name__ == "__main__":
    unittest.main()
