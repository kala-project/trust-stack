"""Exact inline references; these are unit cases, not user acceptance evidence."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the browser binding unit cases")
class InlineBindingTests(unittest.TestCase):
    def test_exact_non_overlapping_bindings_preserve_text_and_ambiguity(self):
        result = subprocess.run(["node", "-e", r'''
const assert = require('node:assert/strict');
const {planBindings, splitTextRanges, buildContentFlow, paginateFlow, paginateBlocks, projectReviewedChapters, safeChartNumber, periodText} = require('./web/report-reader-bindings.js');
const period={start:null,end:null,ranges:[['2024-01-29','2025-01-26'],['2023-01-30','2024-01-28']]};
assert.equal(periodText(period),'Comparison periods: 2024-01-29 to 2025-01-26; 2023-01-30 to 2024-01-28');
assert.equal(periodText({start:'2024-01-01',end:'2024-12-31'}),'2024-01-01 to 2024-12-31');
assert.equal(periodText({start:null,end:null}),'');
for (const value of ['9'.repeat(400), '0.'+'0'.repeat(350)+'1', '9007199254740993', 'NaN', 'Infinity', '', '1,000']) {
 assert.equal(safeChartNumber(value),null, value);
}
assert.equal(safeChartNumber('-17079.0000'),-17079);
assert.equal(safeChartNumber('0.0000'),0);
assert.equal(safeChartNumber('5.94'),5.94);
for (const status of ['unsupported','conflicting']) {
 const saved=[{id:'question-conclusion',blocks:[{type:'question'},
   {id:'original',type:'finding',status,text:'original conclusion'},
   {id:'later',type:'finding',status:'partial_support',text:'management comment'}]},
  {id:'analysis-findings',blocks:[{id:'other',type:'finding',status:'inference'}]},
  {id:'sources',blocks:[]},{id:'boundaries-method',blocks:[]}];
 const frozen=JSON.stringify(saved), projected=projectReviewedChapters(saved);
 assert.equal(JSON.stringify(saved),frozen);
 assert.equal(projected[0].blocks.filter(b=>b.type==='finding').length,0);
 assert.deepEqual(projected[1].blocks.map(b=>b.id),['later','other']);
 assert.equal(projected[3].blocks[0].id,'original');
 assert.equal(projected[3].blocks[0].type,'review_finding');
 assert.equal(projected[0].blocks.filter(b=>b.type==='notice').length,1);
 assert.deepEqual(projectReviewedChapters(projected),projected);
}
const blocks = [{id:'a',text:'a'.repeat(800)}, {id:'b',text:'b'.repeat(800)}, {id:'c',text:'c'.repeat(8000)}];
const pages = paginateBlocks(blocks);
assert.equal(pages.length,3);
assert.deepEqual(pages.flat(),blocks);
assert.equal(pages[2][0].text.length,8000);
assert.deepEqual(paginateBlocks([]),[[]]);
const frozenChapters = [
 {id:'question-conclusion',title:'问题与结论',blocks:[
  {id:'question',type:'question',text:'研究什么？',anchor_ids:[]},
  {id:'long',type:'finding',title:'保存的结论',text:'甲。'.repeat(700)+'锚点数字 42。'+'乙。'.repeat(700),anchor_ids:['claim','number']}]},
 {id:'analysis-findings',title:'分析与发现',blocks:[
  {id:'table',type:'dataset',title:'年度变化',rows:Array.from({length:17},(_,i)=>({id:'p'+i,anchor_id:'p'+i+'-anchor',value:String(i)}))}]},
 {id:'evidence-sources',title:'证据与来源',blocks:[{id:'source',type:'sources',source_ids:[]}]},
 {id:'boundaries-method',title:'边界与方法',blocks:[{id:'limits',type:'limitations',items:['边界一']}]},
];
const frozenAnchors=[{id:'claim',kind:'assertion',text:'不存在于正文的保存判断'},{id:'number',kind:'number',text:'42'}];
const frozenBefore=JSON.stringify({frozenChapters,frozenAnchors});
const flow=buildContentFlow(frozenChapters,frozenAnchors,{maxText:500,maxRows:6});
assert.equal(flow.filter(x=>x.kind==='chapter').length,4);
assert.equal(new Set(flow.filter(x=>x.kind==='chapter').map(x=>x.chapterId)).size,4);
assert.equal(flow.filter(x=>x.kind==='block'&&x.block.id==='long').map(x=>x.block.text).join(''),frozenChapters[0].blocks[1].text);
assert.deepEqual(flow.filter(x=>x.kind==='block'&&x.block.id==='long').flatMap(x=>x.block.anchor_ids).sort(),['claim','number']);
assert.deepEqual(flow.filter(x=>x.kind==='block'&&x.block.id==='table').flatMap(x=>x.block.rows.map(r=>r.id)),Array.from({length:17},(_,i)=>'p'+i));
const fullPages=paginateFlow(flow,850);
assert.deepEqual(fullPages.flat(),flow);
assert(fullPages.some(page=>new Set(page.map(x=>x.chapterId)).size>1),'chapter transitions may share a physical page');
assert.equal(JSON.stringify({frozenChapters,frozenAnchors}),frozenBefore);
for (const count of [2,17]) {
 const lists=[{id:'sources',type:'sources',source_ids:Array.from({length:count},(_,i)=>'s'+i),text:'来源说明。'.repeat(300)},
  {id:'limits',type:'limitations',items:Array.from({length:count},(_,i)=>'限制'+i),text:'限制说明。'.repeat(300)}];
 const original=JSON.stringify(lists);
 const collected=buildContentFlow([{id:'collections',blocks:lists}],[],{maxText:30,maxRows:1}).filter(x=>x.kind==='block');
 assert.equal(collected.length,2,'measured collections must not be pre-split by text length or item count');
 assert.deepEqual(collected.map(x=>x.block),lists);
 assert.equal(JSON.stringify(lists),original);
}
const duplicated='42 '+'甲'.repeat(55)+'42';
const duplicatedAnchor={id:'ambiguous',kind:'number',text:'42'};
const fragmented=buildContentFlow([{id:'analysis',blocks:[{id:'a',type:'finding',text:duplicated,anchor_ids:['ambiguous']}]}],[duplicatedAnchor],{maxText:30});
for(const item of fragmented.filter(x=>x.kind==='block')) {
 const anchors=item.block.anchor_ids.map(()=>duplicatedAnchor);
 const plan=planBindings(item.block.text,anchors,item.block.inline_anchor_ids);
 assert(!plan.segments.some(segment=>segment.anchorId),'fragmentation must not turn an ambiguous number into an exact binding');
}
assert.equal(fragmented.filter(x=>x.kind==='block').flatMap(x=>x.block.anchor_ids).length,1);
for(const budget of [1,2,3,30]) {
 const parts=splitTextRanges('甲😀乙😀',budget);
 assert.equal(parts.map(p=>p.text).join(''),'甲😀乙😀');
 assert(parts.every(p=>p.text.isWellFormed()),'no split surrogate characters');
}
const body = '😀 IBM 7.62 %，NVIDIA 114.20 %。';
const anchors = [
 {id:'whole',kind:'assertion',text:body},
 {id:'ibm',kind:'number',text:'7.62 %'},
 {id:'nvda',kind:'number',text:'114.20 %'},
];
const before = JSON.stringify(anchors);
const plan = planBindings(body, anchors);
assert.equal(plan.segments.map(s=>s.text).join(''),body);
assert.deepEqual(plan.segments.filter(s=>s.anchorId).map(s=>s.anchorId),['ibm','nvda']);
assert.deepEqual(plan.remaining.map(a=>a.id),['whole']);
assert.equal(JSON.stringify(anchors),before);
for (const [text, refs] of [
 ['100 and 100', [{id:'a',kind:'number',text:'100'}]],
 ['100', [{id:'a',text:'100'},{id:'b',text:'100'}]],
 ['1,000', [{id:'a',text:'1000'}]],
 ['no number', [{id:'a',text:'0'}]],
]) {
 const p=planBindings(text,refs);
 assert.equal(p.segments.map(s=>s.text).join(''),text);
 assert.equal(p.segments.filter(s=>s.anchorId).length,0);
 assert.equal(p.remaining.length,refs.length);
}
assert.equal(planBindings('<img src=x onerror=evil()>',[]).segments[0].text,'<img src=x onerror=evil()>');
assert.equal(planBindings('source claim',[{id:'claim',text:'source claim'}]).segments[0].anchorId,'claim');
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
