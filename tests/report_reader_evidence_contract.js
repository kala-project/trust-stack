(function () {
  "use strict";

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  function source(title, value, column, url) {
    return {
      fact_id: "fact-" + column,
      evidence_id: "evidence-" + column,
      source_id: "doc-registered",
      document_version_id: "doc-registered",
      title: title,
      source_url: url || "https://example.test/source.pdf",
      rights_status: "review_required",
      quote: value,
      context: "前文 <script>不会执行</script> 后文",
      locator: {
        table_index: 2,
        row_index: 4,
        column_index: column,
        pdf_provenance: {
          physical_page: 9,
          capability_status: "formally_parsed",
          title: { raw_text: "CONSOLIDATED RESULTS" },
          unit_line: { raw_text: "USD millions" },
          fiscal_period_header: { raw_text: "Year ended 2025" },
          target: { cell: { raw_text: value, bbox: { x_min: 1, y_min: 2, x_max: 3, y_max: 4 } } },
        },
      },
      table_excerpt: {
        rows: [
          { row_index: 0, cells: ["Metric", "2025", "2024"] },
          { row_index: 4, cells: [title, value, "90"] },
        ],
        selected_row: 4,
        selected_column: 1,
      },
    };
  }

  window.runReportEvidenceContract = function () {
    const checks = [];
    function check(condition, message) { assert(condition, message); checks.push(message); }

    check(ReportReaderSecurity.safeExternalUrl("https://example.test/source") === "https://example.test/source", "HTTPS 来源通过统一 URL 白名单");
    check(ReportReaderSecurity.safeExternalUrl("http://example.test/source") === "http://example.test/source", "HTTP 来源通过统一 URL 白名单");
    for (const unsafe of ["javascript:alert(1)", "data:text/html,unsafe", "//evil.example/path", "/relative", "ftp://evil.example/file"]) {
      check(ReportReaderSecurity.safeExternalUrl(unsafe) === null, "危险或非显式 HTTP(S) 来源被拒绝：" + unsafe.split(":", 1)[0]);
    }

    const first = source("Revenue 2025", "100", 1);
    const second = source("Revenue 2024", "90", 2);
    const payload = {
      anchor: {
        text: "fallback claim",
        semantic_review: {
          status: "mixed",
          scope: "contained_claims",
          review_scope: "partial",
          semantic_verified: false,
          claims: [
            { id: "supported", exact_text: "受控支持句", support_status: "supported_by_evidence", reason: "支持理由" },
            { id: "inference", exact_text: "受控推断句", support_status: "inference", reason: "推断理由" },
            { id: "negative", exact_text: "负面 <img src=x onerror=alert(9)>", support_status: "unsupported", reason: "不支持 <script>alert(8)</script>" },
          ],
        },
      },
      evidence: {
        claim: "增长率 <img src=x onerror=alert(1)>",
        verification: { method: "核对公式和两个输入", boundary: "仅限年度比较，尚未人工核准" },
      },
      frozen_detail: {
        traces: [
          {
            status: "available",
            steps: [{ title: "读取两个原表单元格", details: "分别定位，不串混" }],
            calculation: {
              formula: "(operand[0] - operand[1]) / operand[1]",
              value: "11.11",
              unit: "%",
              arithmetic: { operands: [{ metric: "Revenue 2025", value: "100", unit: "USD millions" }, { metric: "Revenue 2024", value: "90", unit: "USD millions" }] },
              limitations: ["仅为算术关系"],
            },
            source: {
              inputs: [
                { fact_id: "fact-1", evidence_id: "evidence-1", metric: "Revenue 2025", value: "100", raw_value: "100", unit: "USD millions", period_start: "2025-01-01", period_end: "2025-12-31" },
                { fact_id: "fact-2", evidence_id: "evidence-2", metric: "Revenue 2024", value: "90", raw_value: "90", unit: "USD millions", period_start: "2024-01-01", period_end: "2024-12-31" },
              ],
              sources: [first, second],
            },
          },
          {
            status: "partial",
            steps: [{ title: "管理层原文", details: "只能支持其表述" }],
            source: {
              title: "危险文字 <svg onload=alert(2)>",
              quote: "原文 <b>不是 HTML</b>",
              context: "上下文 javascript:alert(3)",
              source_url: "javascript:alert(4)",
              locator: { start: 10, end: 40 },
            },
          },
        ],
      },
    };
    const reportPayload = {
      report: { id: "report-safe" },
      sources: [{ id: "doc-registered", document_version_id: "doc-registered", source_sha256: "frozen-hash" }],
    };
    const beforePayload = JSON.stringify(payload);
    const beforeReport = JSON.stringify(reportPayload);
    const root = ReportEvidenceBrowser.render({ payload: payload, reportPayload: reportPayload });
    document.getElementById("fixture").replaceChildren(root);

    check(root.querySelector(".selected-claim") && root.querySelector(".verification-goal"), "顶部直接复用所选主张与核查目标结构");
    check(root.querySelector(".evidence-browser-semantic-review").textContent.includes("model review of selected claims"), "摘要明确模型审核仅覆盖部分主张且不是整段核准");
    check(root.querySelectorAll(".evidence-browser-semantic-claim").length === 3, "可展开明细显示所有已保存子主张");
    check(root.querySelector("[data-semantic-status='unsupported']").textContent.includes("Unsupported"), "负面审核状态在所选主张区域就地可见");
    check(root.querySelector(".evidence-browser-semantic-status").textContent.includes("Unsupported: 1 claim"), "折叠明细时摘要仍显示负面状态");
    check(root.querySelector(".miller") && root.querySelectorAll(".miller-col").length === 4, "四层浏览直接复用 Miller 结构");
    check(root.querySelectorAll(".col-head").length === 4 && root.querySelector(".evidence-menu"), "列头与依据菜单复用 mock 组件类");
    check(root.querySelectorAll(".evidence-browser-column").length === 4, "呈现四个明确层级");
    check(root.querySelectorAll(".evidence-browser-trace").length === 2, "多个 trace 成为可选择依据而非长卡片");
    check(root.querySelectorAll(".evidence-browser-source-choices button").length === 2, "复合计算的底层输入可分别选择");
    check(root.querySelector(".evidence-browser-step-3").textContent.includes("Revenue 2025"), "默认来源与第一个输入一致");
    check(root.querySelector(".evidence-browser-step-4").textContent.includes("File page 9"), "PDF 使用真实物理页中文提示");
    check(root.querySelector(".evidence-browser-step-4").textContent.includes("Source cell: 100"), "PDF 保留原单元格提示");
    check(root.querySelectorAll(".evidence-browser-target-cell").length === 1, "原表切片精确高亮目标单元格");
    check(root.querySelector(".reader-source-download[href^='/api/reports/raw-source?']"), "仅有冻结哈希的登记来源构造 raw-source 下载链接");
    check(root.querySelector("a[href='https://example.test/source.pdf']"), "合法 HTTPS 来源生成可点击链接");
    const openViewer = root.querySelector(".open-source");
    const viewer = root.querySelector(".source-viewer");
    openViewer.focus();
    openViewer.click();
    check(viewer.hidden === false && !viewer.getAttribute("hidden"), "原件放大打开共享 source viewer");
    check(viewer.textContent.includes("not the complete source or full text"), "放大 viewer 明确冻结切片不是全文");
    check(viewer.querySelector("a[href^='/api/reports/raw-source?']"), "放大 viewer 提供真实冻结原件下载");
    const viewerClose = viewer.querySelector("[data-close-source-viewer]");
    check(document.activeElement === viewerClose, "打开原件后焦点进入返回依据按钮");
    viewerClose.click();
    check(viewer.hidden === true && document.activeElement === openViewer, "返回依据关闭 viewer 并恢复原件放大焦点");

    const sourceButtons = root.querySelectorAll(".evidence-browser-source-choices button");
    sourceButtons[1].focus();
    sourceButtons[1].click();
    check(document.activeElement === sourceButtons[1], "切换底层来源不丢失焦点");
    check(root.querySelector(".evidence-browser-step-3").textContent.includes("Revenue 2024"), "切换输入同步更新来源定位");
    check(root.querySelector(".evidence-browser-step-4").textContent.includes("Source cell: 90"), "切换输入同步更新原始资料");

    const traceButtons = root.querySelectorAll(".evidence-browser-trace");
    traceButtons[1].focus();
    traceButtons[1].click();
    check(document.activeElement === traceButtons[1], "切换 trace 不丢失焦点");
    check(traceButtons[1].getAttribute("aria-selected") === "true" && traceButtons[0].getAttribute("aria-selected") === "false", "依据列表具有唯一明确选中项");
    check(root.querySelector(".evidence-browser-step-4").textContent.includes("原文 <b>不是 HTML</b>"), "危险模型文字按纯文本呈现");
    check(!root.querySelector("img, svg, script"), "模型文字不能注入 HTML 元素");
    check(!Array.from(root.querySelectorAll("a")).some(function (link) { return link.href.startsWith("javascript:") || link.href.startsWith("data:"); }), "危险来源链接被拒绝");
    check(!root.querySelector(".evidence-browser-step-4 .reader-source-download"), "未登记来源不构造下载链接");
    check(root.querySelector(".evidence-browser-step-4").textContent.includes("No frozen-source download is currently available"), "原件不可用时明确说明且不补造全文");

    const mobileButtons = root.querySelectorAll(".evidence-browser-mobile-nav button");
    check(mobileButtons.length === 4, "手机提供四步可操作导航");
    mobileButtons[3].click();
    check(root.dataset.mobileStep === "4" && mobileButtons[3].getAttribute("aria-current") === "step", "手机步骤切换更新当前层级");

    const emptyPayload = { evidence: { claim: "无来源", verification: {} }, frozen_detail: { traces: [] } };
    const emptyBefore = JSON.stringify(emptyPayload);
    const emptyRoot = ReportEvidenceBrowser.render({ payload: emptyPayload, reportPayload: {} });
    check(emptyRoot.querySelector(".evidence-browser-semantic-empty").textContent.includes("does not mean the claim was approved"), "旧 payload 缺少审核字段时显示诚实空状态");
    check(emptyRoot.textContent.includes("No expandable evidence path was saved") && emptyRoot.textContent.includes("Source material is unavailable"), "空 trace 与缺来源提供诚实空状态");
    check(JSON.stringify(emptyPayload) === emptyBefore, "空 payload 不被修改");
    check(JSON.stringify(payload) === beforePayload && JSON.stringify(reportPayload) === beforeReport, "渲染和切换不修改输入 payload");

    const scalarPayload = { anchor: { semantic_review: { claim_id: "legacy", reviewed_text: "旧标量审核", support_status: "conflicting", reason: "旧理由" } }, evidence: { claim: "旧载荷", verification: {} } };
    const scalarRoot = ReportEvidenceBrowser.render({ payload: scalarPayload, reportPayload: {} });
    check(scalarRoot.querySelector("[data-semantic-status='conflicting']").textContent.includes("旧标量审核"), "兼容既有标量 semantic_review 字段");

    const reversed = JSON.parse(beforePayload);
    reversed.frozen_detail.traces[0].source.sources.reverse();
    const reversedRoot = ReportEvidenceBrowser.render({ payload: reversed, reportPayload: reportPayload });
    check(reversedRoot.querySelector('.evidence-browser-source-choices button').textContent.includes('Revenue 2024'), '来源乱序时按事实身份而非下标关联输入');
    reversed.frozen_detail.traces[0].source.sources[0].fact_id = 'missing';
    const unmatched = ReportEvidenceBrowser.render({ payload: reversed, reportPayload: reportPayload });
    check(unmatched.querySelector('.evidence-browser-source-choices button').textContent.includes('unbound input'), '身份缺失明确未绑定而不猜测数值归属');
    const noBytes = JSON.parse(beforeReport);
    delete noBytes.sources[0].source_sha256;
    const noBytesRoot = ReportEvidenceBrowser.render({ payload: payload, reportPayload: noBytes });
    check(!noBytesRoot.querySelector("a[href^='/api/reports/raw-source?']") && noBytesRoot.textContent.includes('No frozen-source download is currently available'), '仅登记未保存原件时不给出虚假的下载链接');
    const disabledDownload = JSON.parse(beforeReport);
    disabledDownload.sources[0].raw_download_available = false;
    const disabledRoot = ReportEvidenceBrowser.render({ payload: payload, reportPayload: disabledDownload });
    check(!disabledRoot.querySelector("a[href^='/api/reports/raw-source?']"), '登记来源明确禁止下载时不构造下载链接');

    window.__evidenceContractResult = { ok: true, checks: checks };
    return window.__evidenceContractResult;
  };

  try {
    window.runReportEvidenceContract();
  } catch (error) {
    window.__evidenceContractResult = { ok: false, error: error && (error.stack || error.message || String(error)) };
  }
}());
