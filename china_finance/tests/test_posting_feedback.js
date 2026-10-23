// Run with node --test china_finance/tests/test_posting_feedback.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const root = path.resolve(__dirname, '..');
function harness({fresh = false, dirty = false, workflow = false, preparation = false, ready = false} = {}) {
 const handlers = {}, events = {}, calls = [], buttons = [], messages = [], saves = [], indicators = [];
 let changed = dirty;
 const context = vm.createContext({console, setTimeout: fn => fn(),
  __: (text, args=[]) => text.replace(/\{(\d+)\}/g, (_, i) => args[i]),
  $: () => ({off: () => {}, on: (event, fn) => {events[event] = fn;}}),
  frappe: {ui: {form: {on: (dt, actions) => {handlers[dt] = {...handlers[dt], ...actions};}}},
   utils: {escape_html: value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;')},
   model: {has_workflow: () => workflow, sync: () => {}},
   call: async options => {calls.push(options); const result = {message: options.method.endsWith('get_mode') ? {enabled: preparation} : {preparation_enabled: preparation, ready}}; options.callback?.(result); return result;},
   set_route: (...route) => {context.route = route;},
   msgprint: message => messages.push(message), show_alert() {},
   require: async () => {}, confirm: (_text, yes) => yes(),
  },
 });
 const frm = {doctype: 'Journal Entry', doc: {doctype:'Journal Entry', name:'ACC-JV-1', company:'Company A', posting_date:'2026-07-17', modified:'1', docstatus:0},
  is_new: () => fresh, is_dirty: () => changed, perm: [{create:1, write:1, submit:1}], wrapper: {}, toolbar: {},
  page: {btn_primary:{prop(){}}, set_primary_action(label, action) {this.label=label; this.action=action;}},
  add_custom_button: (label, action) => buttons.push({label, action}),
  save(action = 'Save', callback, _btn, on_error) {saves.push(action); if (frm.fail_save) {on_error(); return Promise.resolve();} changed=false; callback?.({}); return Promise.resolve();},
  savesubmit(_btn, done, on_error) {saves.push('Submit'); if (frm.cancel) on_error(); else done(); return Promise.resolve();},
  script_manager: {trigger: async () => {}}, reload_doc: async () => {},
  dashboard: {stats_area_row:{find: () => ({remove(){}})}, add_indicator: (message, color) => {indicators.push({message,color});return {attr(){}};}},
 };
 vm.runInContext(fs.readFileSync(path.join(root,'public/js/gl_source_snapshot.js'),'utf8'),context);
 vm.runInContext(fs.readFileSync(path.join(root,'public/js/journal_entry_direct_post.js'),'utf8'),context);
 return {context, frm, handlers, calls, buttons, messages, saves, indicators, events,
  dirty(value) {changed=value;events['dirty.china_finance_direct_post']?.();},
  refresh: () => handlers['Journal Entry'].refresh(frm),
  snapshot: () => vm.runInContext('china_finance_source_snapshot',context),
 };
}

for (const source of ['bank','excel','manual']) test(`saved ${source} draft uses native posting with finance wording`, async()=>{
 const h=harness();
 if(source==='bank') h.frm.doc.custom_china_bank_transaction='BANK-1';
 if(source==='excel') h.frm.doc.title='Excel导入：Batch';
 await h.refresh();assert.equal(h.frm.page.label,'记账');await h.frm.page.action();
 assert.deepEqual(h.saves,['Submit']);assert.equal(h.frm.__china_finance_direct_posting,false);
 assert.ok(h.calls.every(call => !call.method.endsWith('save_and_post_journal_entry')));
});

test('unsaved changes are saved before native posting, and dirty event adjusts wording',async()=>{
 const h=harness();await h.refresh();h.dirty(true);assert.equal(h.frm.page.label,'保存并记账');
 await h.frm.page.action();assert.deepEqual(h.saves,['Save','Submit']);
});

test('a failed draft save must not submit or claim success',async()=>{
 const h=harness({dirty:true});h.frm.fail_save=true;await h.refresh();await h.frm.page.action();
 assert.deepEqual(h.saves,['Save']);assert.equal(h.frm.__china_finance_direct_posting,false);assert.ok(h.messages.length);
});

test('native confirmation cancellation releases the posting guard',async()=>{
 const h=harness();h.frm.cancel=true;await h.refresh();await h.frm.page.action();await h.frm.page.action();
 assert.deepEqual(h.saves,['Submit','Submit']);assert.equal(h.frm.__china_finance_direct_posting,false);
});

test('ready preparation draft can post, but changed content must be saved and reviewed again',async()=>{
 const h=harness({preparation:true,ready:true});await h.refresh();assert.equal(h.frm.page.label,'记账');
 h.dirty(true);assert.equal(h.frm.page.label,'保存草稿');await h.frm.page.action();assert.deepEqual(h.saves,['Save']);
});

test('unreviewed preparation draft never exposes direct posting',async()=>{
 const h=harness({preparation:true});await h.refresh();assert.equal(h.frm.page.label,'保存草稿');
 assert.ok(h.buttons.some(button=>button.label==='核对完成，待记账'));
});

test('active workflow and controlled amendment actions remain owned by their existing handlers',async()=>{
 const h=harness({workflow:true});h.frm.page.label='Approve';await h.refresh();h.dirty(true);assert.equal(h.frm.page.label,'Approve');
 const amended=harness();amended.frm.doc.amended_from='OLD';amended.frm.page.label='保存并记账';await amended.refresh();assert.equal(amended.frm.page.label,'保存并记账');
});

test('no native submit permission means no posting action is installed',async()=>{
 const h=harness();h.frm.perm[0].submit=0;await h.refresh();assert.equal(h.frm.page.action,undefined);
});

test('late mode response cannot replace another document action',async()=>{
 const h=harness();let finish;h.context.frappe.call=()=>new Promise(resolve=>finish=resolve);
 const pending=h.refresh();h.frm.doc.name='OTHER';h.frm.page.label='Other';finish({message:{ready:true}});await pending;assert.equal(h.frm.page.label,'Other');
});

test('ledger navigation pins the source and clears stale draft, batch and account filters',()=>{
 const h=harness();h.snapshot().open_ledger(h.frm.doc);const options=h.context.frappe.route_options;
 assert.equal(options.company,'Company A');assert.equal(options.from_date,'2026-07-17');assert.equal(options.to_date,options.from_date);
 assert.equal(options.source_name,'ACC-JV-1');assert.equal(options.voucher_status,'全部有效凭证');assert.equal(options.period_preset,'');
 assert.equal(options.receipt_import,'');assert.equal(options.account,'');assert.equal(options.voucher_word,'');
 assert.deepEqual(h.context.route,['query-report','China Voucher Ledger']);
});

for(const state of [{posted:true,has_gl:true,snapshot_ready:true,statutory_number:'记12'},
 {posted:true,has_gl:true,snapshot_ready:false,reason:'Not ready'}, {posted:false,has_gl:false,snapshot_ready:false}]) test(`result feedback reflects actual posted=${state.posted} snapshot=${state.snapshot_ready}`,async()=>{
 const h=harness();h.context.frappe.call=async()=>({message:state});await h.snapshot().posting_result(h.frm.doc);
 const result=h.messages.at(-1);assert.equal(result.indicator,state.snapshot_ready?'green':'orange');assert.equal(result.primary_action.label,'在查凭证中查看');
 if(state.snapshot_ready) assert.match(result.message,/记12/);else if(state.posted) assert.match(result.message,/已记账.*待补齐/);else assert.match(result.message,/后台记账/);
});

test('failed result read explains uncertainty instead of claiming a failed or repeated posting',async()=>{
 const h=harness();h.context.frappe.call=async()=>{throw Error('network');};await h.snapshot().posting_result(h.frm.doc);
 assert.match(h.messages.at(-1),/勿重复记账/);
});

test('posted source missing a snapshot retains navigation and offers only a permitted repair',()=>{
 const h=harness();h.frm.doc.docstatus=1;h.context.frappe.call=options=>{options.callback({message:{posted:true,has_gl:true,can_repair:true}});return Promise.resolve();};
 h.snapshot().refresh(h.frm);assert.ok(h.buttons.some(button=>button.label==='在查凭证中查看'));assert.ok(h.buttons.some(button=>button.label==='补齐正式凭证'));
 assert.ok(h.indicators.some(row=>row.message==='已记账，正式凭证待补齐'));
});

test('repair confirmation performs one repair request and never submits the source',async()=>{
	const h=harness();h.frm.doc.docstatus=1;let confirm;h.context.frappe.confirm=(_text,yes)=>{confirm=yes;};h.snapshot().repair(h.frm);
 const pending=confirm();await Promise.resolve();h.snapshot().repair(h.frm);await pending;
 assert.equal(h.calls.filter(call=>call.method.endsWith('repair_posting_snapshot')).length,1);assert.deepEqual(h.saves,[]);
});
