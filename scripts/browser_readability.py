"""Real-browser research journeys; injected failures are explicitly test-only."""
import json
import time
from pathlib import Path
from uuid import uuid4


def run_readability(b):
    q = lambda selector: f"document.querySelector({json.dumps(selector)})"
    def click(selector):
        b.click(q(selector))
    def ready(company=None):
        expression = "document.readyState === 'complete' && !!document.querySelector('#content') && !document.querySelector('#content').hidden && document.querySelector('#loading-state').hidden && !!document.querySelector('#company-switcher [data-company-id]') && !document.querySelector('#breadcrumb-company').textContent.includes('载入')"
        if company:
            expression += f" && document.querySelector('#breadcrumb-company').textContent.includes('{company}')"
        b.wait(expression, '研究数据载入完成')
    def nav(view):
        if b.evaluate("innerWidth <= 760 && !document.body.classList.contains('nav-open')"):
            b.wait("document.querySelector('#sidebar-menu').getBoundingClientRect().right<=1", '移动导航收起动画完成')
            click('#menu-button')
            b.wait("Math.abs(document.querySelector('#sidebar-menu').getBoundingClientRect().left)<1", '移动导航展开动画完成')
        click(f'[data-view={view}]')
        b.wait(f"!document.querySelector('#view-{view}').hidden", '目标页面确实可见')
    def company(co):
        if b.evaluate("innerWidth <= 760 && !document.body.classList.contains('nav-open')"):
            b.wait("document.querySelector('#sidebar-menu').getBoundingClientRect().right<=1", '移动导航收起动画完成')
            click('#menu-button')
            b.wait("Math.abs(document.querySelector('#sidebar-menu').getBoundingClientRect().left)<1", '移动公司导航展开完成')
        click(f'[data-company-id={co}]')
        ready(co)
    def snapshot(name):
        b.screenshot(name)
        (b.output / f'{name}.txt').write_text(b.evaluate("document.body.innerText"), encoding='utf-8')
    def no_technical_dump(selector):
        return b.evaluate(f"!/(claim-[a-f0-9]{{12}}|fact-[a-f0-9]{{12}}|SELECT fact_id|eps_diluted|operating_income|segment_revenue)/.test({q(selector)}.innerText)")
    def failure(prefix):
        b.evaluate(f"window.__realFetch ||= window.fetch; window.fetch=(url,opts)=>String(url).startsWith({json.dumps(prefix)}) ? Promise.resolve(new Response('{{}}',{{status:503}})) : window.__realFetch(url,opts)")
    def restore():
        b.evaluate("window.fetch=window.__realFetch || window.fetch")

    html_reports = []
    ready()
    company('MSFT')
    nav('overview')
    b.check(b.evaluate("document.querySelector('#research-brief').innerText.includes('18.10%')"), '首页直接展示真实收入同比结果，而非档案清单')
    b.check(b.evaluate("document.querySelector('#research-brief').innerText.includes('ROI') && document.querySelector('#research-brief').innerText.includes('17%')"), '首页同时提示ROI不足与固定汇率差别')
    b.check(b.evaluate("!document.querySelector('#stats-grid').open && !document.querySelector('#overview-sources').open"), '技术统计和哈希默认折叠')
    b.check(no_technical_dump('#view-overview'), '首页可见文字无内部ID或原始指标字段')
    b.check(b.evaluate("!document.querySelector('#as-of').textContent.includes('10/30')"), '微软时点不混入苹果披露日期')
    nav('chat')
    b.wait("chatState.statusKnown", '真实提供方状态呈现')
    b.check(b.evaluate("(()=>{const input=document.querySelector('#chat-input');const original=input.value;input.value='阅读回归草稿，不发送';input.dispatchEvent(new Event('input'));const enabled=!document.querySelector('#send-message').disabled;input.value=original;input.dispatchEvent(new Event('input'));const guide=document.querySelector('#chat-setup-guide');return enabled===chatState.available && (chatState.available ? document.querySelector('#chat-provider-badge').textContent.includes('可发送') && !guide : !!guide && guide.innerText.includes('无需额外确认开关')) && document.querySelector('[data-view=chat]').getAttribute('aria-current') === 'page'})()"), '对话发送状态与真实提供方一致：已登录可发送，未认证说明原因且不要求额外人工开关')
    nav('overview')
    snapshot('desktop-home.png')
    click('#research-brief [data-go-view]')
    b.check(b.evaluate("[...document.querySelectorAll('.claim-card h3')].slice(0,3).map(x=>x.textContent).join('|')") == '收入同比|营业利润同比|稀释每股收益同比', '结论按业务指标排列而非哈希ID')
    for name, count in [('verified_calculation',3),('source_reported',0),('attention',3),('all',6)]:
        click(f'[data-filter={name}]')
        b.check(b.evaluate("document.querySelectorAll('#claims-table .claim-card').length") == count, f'微软筛选{name}：{count}条')
    b.check(no_technical_dump('#claims-table'), '结论正文为中文业务表达，没有字段与SQL')
    snapshot('desktop-conclusions.png')

    for co, count in [('MSFT',6),('AAPL',1)]:
        company(co); nav('claims')
        cards = b.evaluate("[...document.querySelectorAll('#claims-table [data-claim-id]')].map(x=>({id:x.dataset.claimId,title:x.closest('article').querySelector('h3').textContent}))")
        b.check(len(cards) == count, f'{co}全部结论可见')
        for item in cards:
            selector = f'#claims-table [data-claim-id="{item["id"]}"]'
            click(selector)
            b.wait("document.querySelector('#evidence-dialog').open && !!document.querySelector('.complete-json pre')", '逐条证据载入')
            payload = b.evaluate("JSON.parse(document.querySelector('.complete-json pre').textContent)")
            b.check(payload['claim']['claim_id'] == item['id'], f'{co} {item["title"]}：证据对应正确结论')
            b.check(no_technical_dump('#evidence-content'), f'{co} {item["title"]}：默认不暴露工程字段')
            b.check(b.evaluate("[...document.querySelectorAll('#evidence-content pre.code-block')].every(x=>x.closest('details') && !x.closest('details').open)"), f'{co} {item["title"]}：技术JSON按需展开')
            limits = payload['claim'].get('limitations', [])
            shown = b.evaluate("document.querySelector('.claim-hero').innerText")
            b.check(all(limit in shown for limit in limits), f'{co} {item["title"]}：限制不被折叠隐藏')
            if payload['claim']['kind'] == 'calculation':
                a = next(a for a in payload['activities'] if a['type'] == 'sql_calculation')
                visible = b.evaluate("document.querySelector('#evidence-content').innerText")
                b.check(str(a['outputs'][0]) in visible and all(str(row['value']) in visible for row in a['parameters']['all_results']), f'{item["title"]}：公式操作数和真实输出均可读')
            click('.complete-json > summary')
            b.check(b.evaluate("document.querySelector('.complete-json').open"), f'{co} {item["title"]}：完整证据链仍可展开')
            click('.complete-json > summary')
            if payload['evidence']:
                quote = b.evaluate("document.querySelector('.quote-card blockquote').textContent")
                click('.quote-card [data-source-id]')
                b.wait("!document.querySelector('#source-reader').hidden && !!document.querySelector('#source-text mark')", '原文定位')
                b.check(b.evaluate("document.querySelector('#source-text mark').textContent") == quote, f'{co} {item["title"]}：引用精确定位至原文')
                b.check(b.evaluate("[...document.querySelector('#source-text').childNodes].every(n=>n.nodeType===3 || n.tagName==='MARK')"), '来源安全文本无可执行HTML')
                click('#back-to-evidence')
                b.wait("document.querySelector('#evidence-dialog').open && !!document.querySelector('.complete-json pre')", '返回原结论')
                b.check(b.evaluate("JSON.parse(document.querySelector('.complete-json pre').textContent).claim.claim_id") == item['id'], '返回保持原结论而非丢失上下文')
            b.escape()
            b.wait("!document.querySelector('#evidence-dialog').open", '关闭证据')
            b.wait(f"document.activeElement?.dataset.claimId === {json.dumps(item['id'])}", '关闭事件完成后恢复焦点')
            b.check(b.evaluate("document.activeElement?.dataset.claimId") == item['id'], 'Escape后焦点回到当前结论按钮')
        nav('wiki')
        b.check(b.evaluate("document.querySelectorAll('#wiki-sections .wiki-section').length") == count and no_technical_dump('#view-wiki'), f'{co}研究笔记是可读结论，不是ID链接列表')
        for index in range(count):
            b.click(f"document.querySelectorAll('#wiki-sections [data-claim-id]')[{index}]")
            b.wait("!!document.querySelector('.complete-json pre')", '笔记证据')
            b.check(b.evaluate("document.querySelector('#evidence-dialog').open"), f'{co}笔记第{index+1}条证据可打开')
            b.escape()
        snapshot(f'{co.lower()}-notes.png')
        nav('sources')
        b.wait("document.querySelector('#library-status').textContent.includes('失败') || !document.querySelector('#library-status').textContent.includes('正在读取')", '资料库返回明确状态')
        library_text = b.evaluate("document.querySelector('#library-status').textContent")
        b.check(co in b.evaluate("document.querySelector('#sources-title').textContent") and ("失败" not in library_text or "不表示零份" in library_text), f'{co}资料库明确公司且失败不冒充零资料')
        # Curated example sources remain separately readable from their dedicated API; they are not relabeled as the current document library.
        sources = b.evaluate("relevantSources().map(x=>x.source_id)")
        b.check(len(sources) == (5 if co=='MSFT' else 2), f'{co}示例来源范围隔离')
        for source in sources:
            b.evaluate(f"openSource({json.dumps(source)})")
            b.wait("!document.querySelector('#source-reader').hidden && !document.querySelector('#source-reader-title').textContent.includes('载入中')", '逐个示例来源阅读')
            b.check(b.evaluate("document.querySelector('#source-text').textContent.length") > 50, f'来源{source}可阅读真实正文')
            b.check(b.evaluate("!document.querySelector('#source-tables').open"), '解析表格默认收起，避免重复铺陈')
            click('#close-source')
        nav('overview')
        if co == 'AAPL':
            brief = b.evaluate("document.querySelector('#research-brief').innerText")
            b.check('13%' in brief and '调整后' in brief and 'PDF' in brief and '起始日' in brief, '苹果摘要保留调整后比较、PDF缺失及时间未知')
        click('#report-button')
        b.check(b.evaluate("document.activeElement.id") == 'download-html', '导出优先可读简报而非JSON')
        b.escape()
        for fmt in ('html','json'):
            # Actual browser download to an isolated local test directory, not public distribution.
            directory = (b.output / f'downloads-{co}-{fmt}-{uuid4().hex[:8]}').resolve()
            directory.mkdir(exist_ok=True)
            existing = set(directory.iterdir())
            b.cdp.call('Browser.setDownloadBehavior', {'behavior':'allow','downloadPath':str(directory)})
            click('#report-button'); click(f'#download-{fmt}')
            deadline = time.monotonic()+b.timeout
            downloaded = []
            while time.monotonic()<deadline:
                downloaded = [p for p in directory.iterdir() if p.suffix == '.'+fmt and p not in existing]
                if downloaded: break
                time.sleep(.1)
            b.check(bool(downloaded), f'{co} {fmt}真实下载成功')
            contents = downloaded[-1].read_text()
            if fmt == 'json':
                data=json.loads(contents)
                b.check(data['company']['ticker']==co and data['status']=='unreviewed' and bool(data['evidence_chains']), f'{co}JSON保留公司、未审核状态和证据链')
            else:
                b.check('技术核验记录' in contents and '<details>' in contents and '未审核' in contents, f'{co}HTML简报有折叠技术记录和未审核提示')
                html_reports.append((co, downloaded[-1]))
            b.escape()

    nav('wiki'); click('#refresh-button'); ready('AAPL')
    b.check(b.evaluate("!document.querySelector('#view-wiki').hidden"), '刷新保留苹果与研究笔记上下文')
    nav('claims'); click('[data-filter=attention]'); company('MSFT')
    b.check(b.evaluate("document.querySelector('[data-filter=all]').getAttribute('aria-pressed')") == 'true', '公司切换重置筛选，避免假空结果')

    # Failures are injected only in this browser, never alter the stored dataset.
    failure('/api/company?id=AAPL'); company_button='[data-company-id=AAPL]'; click(company_button)
    b.wait("!document.querySelector('#global-notice').hidden", '公司失败提示')
    b.check(b.evaluate("document.querySelector('#content').hidden && document.querySelector('#report-button').disabled && !document.querySelector('#download-html').hasAttribute('href')"), '公司加载失败不展示旧结论、不允许导出')
    restore(); click('#retry-button'); ready('AAPL')
    b.check(b.evaluate("document.querySelector('#global-notice').hidden"), '公司失败可一键恢复')
    failure('/api/health'); click('#refresh-button')
    b.wait("!document.querySelector('#global-notice').hidden", '健康检查失败')
    b.check(b.evaluate("document.querySelector('#report-button').disabled && document.querySelector('#content').hidden"), '完整性失败暂停结果与导出，不假称正常')
    restore(); click('#retry-button'); ready('AAPL')
    failure('/api/overview'); click('#refresh-button')
    b.wait("!document.querySelector('#global-notice').hidden", '首页失败提示')
    b.check(b.evaluate("!document.querySelector('#retry-button').hidden"), '首页错误有明确重试操作')
    restore(); click('#retry-button'); ready('AAPL')
    nav('claims'); failure('/api/evidence'); click('#claims-table [data-claim-id]')
    b.wait("!document.querySelector('#evidence-error').hidden", '证据失败提示')
    b.check(b.evaluate("document.querySelector('#evidence-content').children.length===0"), '证据失败不遗留上一结论')
    restore(); b.escape(); click('#claims-table [data-claim-id]')
    b.wait("!!document.querySelector('.quote-card')", '证据重开成功')
    failure('/api/source'); click('.quote-card [data-source-id]')
    b.wait("document.querySelector('#source-reader-title').textContent==='来源读取失败'", '来源失败提示')
    b.check(b.evaluate("!document.querySelector('#back-to-evidence').hidden"), '来源失败仍可返回结论，不陷入死路')
    restore(); click('#back-to-evidence'); b.wait("!!document.querySelector('.complete-json pre')", '返回证据'); b.escape()

    # A delayed source response must not resurrect a closed reader.
    nav('sources')
    b.evaluate("window.__realFetch ||= window.fetch; window.fetch=async (url,opts)=>{const response=await window.__realFetch(url,opts);if(String(url).startsWith('/api/source'))await new Promise(r=>setTimeout(r,350));return response}")
    b.evaluate("openSource(relevantSources()[0].source_id)"); click('#close-source'); time.sleep(.5)
    b.check(b.evaluate("document.querySelector('#source-reader').hidden"), '关闭后迟到的来源响应不会复活正文')
    restore()
    b.check(b.evaluate("(()=>{const n=document.createElement('pre');const m=appendHighlightedText(n,'A😀中B',{start:1,end:3});return m.textContent==='😀中'&&n.textContent==='A😀中B'})()"), 'Unicode字符定位不拆分emoji')
    b.check(b.evaluate("!readableActivity({parameters:{formula:'unknown',all_results:[{value:2},{value:3}]},outputs:['50']}).includes('同比增长率 =')"), '未知公式不会擅自套用同比解释')

    for width in (390, 768):
        b.cdp.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':844,'deviceScaleFactor':1,'mobile':width==390})
        for view in ('overview','claims','wiki','sources'):
            nav(view)
            b.check(b.evaluate("document.documentElement.scrollWidth<=document.documentElement.clientWidth"), f'{width}px {view}无整页水平溢出')
        nav('claims'); click('#claims-table [data-claim-id]'); b.wait("!!document.querySelector('.complete-json pre')", '窄屏证据')
        b.check(b.evaluate("document.querySelector('#evidence-dialog').getBoundingClientRect().width<=innerWidth"), f'{width}px证据面板不超出视口')
        snapshot(f'evidence-{width}.png'); b.escape(); nav('overview'); snapshot(f'home-{width}.png')
    for co, path in html_reports:
        b.cdp.call('Page.navigate', {'url':path.as_uri()})
        b.wait(f"location.protocol === 'file:' && document.readyState === 'complete' && document.querySelector('h1')?.textContent.includes('{co}')", '实际打开下载的HTML简报')
        b.check(no_technical_dump('main'), f'{co}下载HTML实际渲染主文无原始字段或SQL')
        b.check(b.evaluate("document.querySelector('h2').textContent === '关键结论' && !document.querySelector('details').open"), f'{co}下载简报先展示结论，审计记录默认收起')
        b.check(b.evaluate("document.querySelector('main').innerText.includes('未审核')"), f'{co}离线简报保留未审核提示')
        click('details > summary')
        b.check(b.evaluate("document.querySelector('details').open && document.querySelector('pre').textContent.includes('evidence_chains')"), f'{co}离线完整证据链可展开')
        click('details > summary')
        snapshot(f'{co.lower()}-report.png')
    exceptions=[e for e in b.cdp.events if e.get('method')=='Runtime.exceptionThrown']
    logs=[e.get('params',{}).get('entry',{}) for e in b.cdp.events if e.get('method')=='Log.entryAdded']
    violations=[e for e in logs if 'content security policy' in e.get('text','').lower() and 'refused' in e.get('text','').lower()]
    b.check(not exceptions, f'无未捕获JS异常：{len(exceptions)}')
    b.check(not violations, f'无CSP拒绝：{len(violations)}')
