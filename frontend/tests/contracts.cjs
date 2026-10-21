const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { ApiError, fetchJson, createResearchApi } = require('../.cache/test-build/data/api.js');
const r = require('../.cache/test-build/data/research.js');
const route = require('../.cache/test-build/data/route.js');
const { safeReaderHref } = require('../.cache/test-build/app/links.js');

const conversationPayload = () => {
  const digest = 'a'.repeat(64);
  const correction = {
    correction_id:'correction_1',old_message_id:'message-1',new_message_id:'message-2',
    start:0,end:4,original_text:'old ',explanation:'Outdated input',impact:'Estimate changed',
    created_at:'2026-01-03T00:00:00+00:00',old_version:1,new_version:2,
    evidence_refs:[{kind:'document',id:'document-1'}],semantic_verified:false,
  };
  return {
    conversation_id:'conversation-production',company_id:'company-a',title:'Production conversation',
    created_at:'2026-01-01T00:00:00+00:00',display_question:'Compare the plans',display_task_id:'task-1',
    execution_scope:{version:'research-execution-scope/1',company_ids:['company-a','company-b'],scope_id:`scope-${digest}`,payload_sha256:digest},
    messages:[
      {message_id:'request-1',role:'user',content:'Compare the plans',created_at:'2026-01-01T00:00:00+00:00',run_id:'run-1'},
      {message_id:'message-1',role:'assistant',content:'old report',created_at:'2026-01-02T00:00:00+00:00',run_id:'run-1',status:'completed',citations:[{source:'document-1'}],verification:{state:'saved'}},
      {message_id:'message-2',role:'assistant',content:'new report',created_at:'2026-01-03T00:00:00+00:00',run_id:'run-2',status:'completed'},
    ],
    decision:{
      decision_id:'decision-1',run_id:'run-1',base_message_id:null,request_message:'Compare the plans',kind:'model',
      question:'Choose a model',explanation:'The model changes the estimate',
      options:[{id:'forecast',label:'Forecast',benefit:'Forward view',limitation:'Model risk',forecast:{horizon:2},forecast_details:{baseline:{value:'10'}}}],
      multiple:false,groups:[],created_at:'2026-01-02T00:00:00+00:00',status:'pending',selected_ids:[],notes:null,continuation_run_id:null,
    },
    report_versions:[
      {version:1,message_id:'message-1',run_id:'run-1',created_at:'2026-01-02T00:00:00+00:00',corrected_by:[correction],corrections:[]},
      {version:2,message_id:'message-2',run_id:'run-2',created_at:'2026-01-03T00:00:00+00:00',corrected_by:[],corrections:[correction]},
    ],
    research_scope:{scope_type:'frozen_per_run',latest_snapshot:null,runs:[],newest_available:{available:false,reason:'fixture'}},
  };
};

test('unresolved intent coverage is unknown, not zero or a render exception', () => {
  assert.equal(r.describeCoverage({documents:null,facts:null}),'Source coverage statistics are not available yet.');
  assert.match(r.describeCoverage({documents:{enrolled:0},facts:{available:false,count:0}}),/0 documents were available and 0 fact records were available/);
  assert.match(r.describeCoverage({documents:null,facts:{available:true,count:9}}),/document count was not recorded and 9 fact records were available/);
  assert.match(r.describeCoverage({documents:{enrolled:3},facts:null}),/3 documents were available and the fact count was not recorded/);
});

test('method detail display preserves exact inputs and rejects incomplete or unknown shapes', () => {
  const x={metric:'Other X',value:'9007199254740993.1234',unit:'EUR',basis:null,period_start:null,period_end:null};
  const d={method:'OLS',equation:'Y = a + b X',sample_count:1,lag_years:0,y_transform:'level',samples:[{x,y:{...x,metric:'Other Y'},y_prior:null}],limitations:['Small sample']};
  const before=JSON.stringify(d);
  assert.equal(r.readRegressionDetails(d).samples[0].x.value,x.value);
  assert.equal(JSON.stringify(d),before);
  for (const bad of [null,{}, {...d,sample_count:2}, {...d,y_transform:{}}, {...d,y_transform:'future'}, {...d,y_transform:'percent_change'}, {...d,samples:[{...d.samples[0],x:{...x,value:12}}]}]) assert.equal(r.readRegressionDetails(bad),null);
  assert.equal(r.readRegressionDetails({...d,y_transform:'percent_change',samples:[{...d.samples[0],y_prior:{...x,value:'-3'}}]}).samples[0].y_prior.value,'-3');
});

test('task identity and route encoding round trip', () => {
  const value = {page:'research', task:'task/a&b', conversation:'会话?1', tab:'answer'};
  assert.deepEqual(route.parseAppRoute(route.formatAppRoute(value)), value);
  assert.equal(route.parseAppRoute('#/unowned'), null);
});
test('late response owner is rejected after task or generation changes', () => {
  const a={sequence:1,navigation:2,taskId:'a',conversationId:'c'};
  assert.equal(r.requestOwnerMatches(a,{...a}),true);
  for (const b of [{...a,taskId:'b'},{...a,sequence:2},{...a,navigation:3},null]) assert.equal(r.requestOwnerMatches(a,b),false);
});
test('unknown task submit retains immutable question and request ID', () => {
  const a=r.prepareTaskSubmission(' question '), b=r.markSubmissionUnknown(a);
  assert.equal(r.prepareTaskSubmission('question',b),b);
  assert.equal(b.question,'question'); assert.equal(b.requestId,a.requestId);
});
test('unknown decision submit keeps the same identity only for exact payload', () => {
  const a=r.markSubmissionUnknown(r.prepareDecisionSubmission('d',['one'],'note','accepted'));
  assert.equal(r.prepareDecisionSubmission('d',['one'],'note','accepted',a),a);
  assert.notEqual(r.prepareDecisionSubmission('d',[],'note','declined',a).requestId,a.requestId);
});
test('transport ambiguity differs from a confirmed validation rejection', () => {
  assert.equal(r.submissionOutcomeUnknown(new TypeError('network'),true,false),true);
  assert.equal(r.submissionOutcomeUnknown(new ApiError(503,{}),true,false),true);
  assert.equal(r.submissionOutcomeUnknown(new ApiError(400,{}),true,false),false);
  assert.equal(r.submissionOutcomeUnknown(new TypeError('after acknowledgment'),true,true),false);
});
test('future run states and HTTP timeouts are not guessed as final outcomes', () => {
  assert.equal(r.presentRun({status:'future_state'}).state,'unknown');
  assert.equal(r.submissionOutcomeUnknown(new ApiError(408,{}),true,false),true);
});
test('completed run alone is not a formal report', () => {
  const x=r.presentRun({status:'completed',run_id:'r',conversation_id:'c'});
  assert.equal(x.hasFormalReport,false); assert.match(x.label,/No formal report yet/);
  assert.equal(r.reportDestination({task_id:'t'},'#/research?task=t').kind,'research_process');
});
test('verification lifecycle is independent of completed author execution and content limitations', () => {
  const base={candidate_id:'candidate-1',conversation_id:'c',author_run_id:'r',verify_job_id:'j',question:'q',scope:{},base_message_id:null,formal_message_id:null,semantic_truth_guaranteed:false,versions:[{version_id:'v1'}]};
  assert.equal(r.presentVerification({...base,status:'queued'}).state,'awaiting_verification');
  assert.equal(r.presentVerification({...base,status:'running'}).state,'verifying');
  assert.equal(r.presentVerification({...base,status:'running',versions:[{version_id:'v1'},{version_id:'v2'}]}).state,'revising');
  assert.match(r.presentVerification({...base,status:'verified_with_limitations',formal_message_id:'m'}).label,/content limitations/);
  assert.equal(r.presentVerification({...base,status:'unresolved'}).state,'incomplete');
});
test('candidate cancellation sends stable conversation and candidate identities once', async () => {
  const original=global.fetch; let captured;
  global.fetch=async(url,init)=>{captured={url,init};return new Response('{"candidate_id":"candidate-1","status":"cancelled"}',{status:200});};
  try {
    await createResearchApi().cancelVerification('conversation-1','candidate-1');
    assert.equal(captured.url,'/api/verification/cancel');
    assert.deepEqual(JSON.parse(captured.init.body),{conversation_id:'conversation-1',candidate_id:'candidate-1'});
  } finally {global.fetch=original;}
});
test('pending decision is not generic blocked failure', () => {
  assert.equal(r.presentRun({status:'blocked',termination_reason:'confirmation_required',decision:{status:'pending'}}).state,'awaiting_decision');
});
test('explicit user cancel is distinguished from unexpected interruption', () => {
  assert.equal(r.presentRun({status:'cancelled',cancel_requested:true}).tone,'warning');
  assert.equal(r.presentRun({status:'cancelled'}).tone,'bad');
});
test('report links reject external hosts and arbitrary paths', () => {
  const origin='http://127.0.0.1:5433';
  assert.equal(safeReaderHref('/report-reader.html?report=a',origin),'/report-reader.html?report=a');
  for(const link of ['javascript:alert(1)','//evil.example/report-reader.html','/api/chat/cancel','https://evil.example/report-reader.html']) assert.equal(safeReaderHref(link,origin),null);
});
test('POST is never retried by data adapter', async () => {
  const original=global.fetch; let calls=0;
  global.fetch=async()=>{ calls++; throw new TypeError('controlled network failure'); };
  try { await assert.rejects(fetchJson('/api/test',{method:'POST',body:{request_id:'r'}})); assert.equal(calls,1); }
  finally { global.fetch=original; }
});
test('authentication error preserves 401 and notifies the caller', async () => {
  const original=global.fetch; let notified=0;
  global.fetch=async()=>new Response('{"error":"authentication_required"}',{status:401});
  try { await assert.rejects(fetchJson('/api/test',{onAuthenticationRequired:e=>{assert.equal(e.status,401);notified++;}}),ApiError); assert.equal(notified,1); }
  finally { global.fetch=original; }
});
test('getConversation decodes the complete production response shape from fetch', async () => {
  const original=global.fetch; const payload=conversationPayload(); let captured;
  global.fetch=async(url,init)=>{captured={url,init};return new Response(JSON.stringify(payload),{status:200});};
  try {
    const decoded=await createResearchApi().getConversation('conversation-production');
    assert.equal(captured.url,'/api/chat/conversation?id=conversation-production');
    assert.deepEqual(decoded,payload);
    assert.deepEqual(decoded.report_versions[0].corrected_by[0].evidence_refs,[{kind:'document',id:'document-1'}]);
    assert.deepEqual(decoded.decision.options[0].forecast_details,{baseline:{value:'10'}});
  } finally {global.fetch=original;}
});
test('getConversation rejects missing, unknown, and mistyped response fields at every typed boundary', async () => {
  const mutations=[
    value=>{delete value.title;},
    value=>{value.unexpected=true;},
    value=>{value.messages[0].content=42;},
    value=>{value.messages[1].citations={source:'document-1'};},
    value=>{value.decision.options[0].unexpected=true;},
    value=>{delete value.decision.base_message_id;},
    value=>{value.report_versions[0].corrected_by[0].old_version='1';},
    value=>{value.report_versions[0].corrected_by[0].evidence_refs[0].extra=true;},
    value=>{value.execution_scope.company_ids=['company-b','company-a'];},
    value=>{value.execution_scope.payload_sha256='A'.repeat(64);},
    value=>{value.execution_scope.scope_id=`scope-${'b'.repeat(64)}`;},
  ];
  const original=global.fetch;
  try {
    for (const mutate of mutations) {
      const payload=conversationPayload(); mutate(payload);
      global.fetch=async()=>new Response(JSON.stringify(payload),{status:200});
      await assert.rejects(createResearchApi().getConversation('conversation-production'),/invalid (conversation|execution scope)/);
    }
  } finally {global.fetch=original;}
});
test('create task transmits exact request identity and question', async () => {
  const original=global.fetch; let captured;
  global.fetch=async(url,init)=>{ captured={url,init};return new Response('{"task_id":"t"}',{status:200}); };
  try {
    await createResearchApi().createTask({question:'real user question',requestId:'stable-r',contextConversationId:null});
    assert.equal(captured.url,'/api/research/tasks');
    assert.deepEqual(JSON.parse(captured.init.body),{question:'real user question',context_conversation_id:null,request_id:'stable-r'});
  } finally { global.fetch=original; }
});
test('retry intent sends only stable task and expected scope version once', async () => {
  const original=global.fetch; let captured; let calls=0;
  global.fetch=async(url,init)=>{calls++;captured={url,init};return new Response('{"task_id":"t","current_scope_version":2}',{status:200});};
  try {
    await createResearchApi().retryIntent({taskId:'task-original',expectedScopeVersion:7});
    assert.equal(calls,1);
    assert.equal(captured.url,'/api/research/tasks/retry-intent');
    assert.deepEqual(JSON.parse(captured.init.body),{task_id:'task-original',expected_scope_version:7});
  } finally {global.fetch=original;}
});
test('retry intent eligibility and React branch are limited to exact parser failures', () => {
  const task=(status,code)=>({task_id:'t',status,code});
  assert.equal(r.canRetryIntent(task('model_unavailable','intent_model_unavailable')),true);
  assert.equal(r.canRetryIntent(task('needs_review','intent_schema_invalid')),true);
  assert.equal(r.canRetryIntent(task('ready','scope_ready')),false);
  assert.equal(r.canRetryIntent(task('needs_review','other_review')),false);
  const workspace=readFileSync(path.join(__dirname,'../src/features/research/ResearchWorkspace.tsx'),'utf8');
  assert.match(workspace,/Reinterpret original question/);
  assert.match(workspace,/canRetryIntent\(task\)/);
  assert.match(workspace,/will not start automatically/);
});
test('unresolved decisions keep independent recovery records across task switches', () => {
  const original=global.sessionStorage, values=new Map();
  global.sessionStorage={getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
  const recovery=require('../.cache/test-build/features/research/mutationRecovery.js');
  const record=id=>({decisionId:id,pending:{state:'unknown',requestId:'request-'+id,signature:'s'},payload:{action:'accepted',selected:['choice'],notes:'n'}});
  try {
    recovery.storeDecisionSubmission(record('one'));recovery.storeDecisionSubmission(record('two'));
    assert.equal(recovery.readDecisionSubmission('one').pending.requestId,'request-one');
    recovery.storeDecisionSubmission(null,'two');
    assert.equal(recovery.readDecisionSubmission('two'),null);
    assert.equal(recovery.readDecisionSubmission('one').pending.requestId,'request-one');
  } finally {global.sessionStorage=original;}
});
