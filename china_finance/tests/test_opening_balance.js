// node --test china_finance/tests/test_opening_balance.js
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const script = path.resolve(__dirname, '../public/js/china_opening_balance.js');
const api = fs.existsSync(script) ? require(script) : {};
const batch = (overrides = {}) => ({ name:'OB-1',company:'公司甲',opening_date:'2026-01-01',currency:'CNY',mode:'New',status:'Draft',version:'v1',rows:[{row_id:'r1',account:'1001 - 库存现金',debit:'0.10',credit:'0',account_currency:'CNY'}],...overrides });
const envelope = (overrides = {}) => ({company:'公司甲',currency:'CNY',opening_date:'2026-01-01',accounts:[{name:'1001 - 库存现金',account_number:'1001',account_name:'库存现金',account_currency:'CNY'},{name:'1122 - 应收账款',account_number:'1122',account_name:'应收账款',account_currency:'CNY'}],dimensions:['cost_center'],permissions:{write:true,activate:true},history_candidates:[],batch:batch(),...overrides});
const session = (call = async () => envelope()) => { const s = new api.Session({call}); s.adopt(envelope()); return s; };

test('opening balance exposes the actual grid component and session', () => {
 assert.equal(typeof api.Session,'function','opening grid session missing'); assert.equal(typeof api.mount,'function');
});
test('whole-batch and filtered totals use exact decimal cents independently', () => {
 const s=session(); s.batch.rows.push({row_id:'r2',debit:'0.20',credit:'0.30'},{row_id:'r3',debit:'0',credit:'0'});
 assert.deepEqual(api.totals(s.batch.rows),{debit:'0.30',credit:'0.30',difference:'0.00',invalid:0});
 s.filters.nonzero=true; assert.equal(s.visible_rows().length,2); s.filters.errors=true; s.preview={errors:[{row_id:'r2',field:'account',message:'请核对'}]};
 assert.equal(s.visible_rows().length,1); assert.equal(api.totals(s.visible_rows()).debit,'0.20'); assert.equal(api.totals(s.batch.rows).debit,'0.30');
 assert.equal(api.to_minor('1.001'),null); assert.equal(api.to_minor('1,234.50'),123450);
});
test('500 Excel TSV rows fill in one batch and preserve zero and quoted multiline notes', () => {
 const s=session(); const lines=Array.from({length:500},(_,i)=>`1001 - 库存现金\t\t\tCNY\t${i}\t0\t备注${i}`);
 assert.equal(s.paste(lines.join('\n'),'r1','account'),500); assert.equal(s.batch.rows.length,500); assert.equal(s.batch.rows[499].debit,'499'); assert.equal(s.batch.rows[499].remarks,'备注499');
 assert.equal(s.batch.rows[0].debit,'0'); assert.equal(s.dirty,true);
 assert.deepEqual(api.parse_tsv('a\t"两行\n说明"\r\nb\t"引号""内容"'),[['a','两行\n说明'],['b','引号"内容']]);
});
test('prefill adds missing company accounts without changing entered values; selected delete is exact', () => {
 const s=session(); s.batch.rows[0].remarks='人工'; assert.equal(s.prefill(),1); assert.equal(s.batch.rows[0].remarks,'人工'); assert.equal(s.prefill(),0);
 s.selected.add(s.batch.rows[1].row_id); assert.equal(s.delete_selected(),1); assert.equal(s.batch.rows[0].row_id,'r1');
});
test('late company reads cannot overwrite newer company or accepted local edits', async () => {
 const requests=[]; const s=new api.Session({call:(_m,args)=>new Promise(resolve=>requests.push({args,resolve}))});
 const a=s.load('公司甲'); const b=s.load('公司乙'); requests[1].resolve(envelope({company:'公司乙',batch:batch({company:'公司乙',name:'B'})})); await b;
 requests[0].resolve(envelope()); await a; assert.equal(s.company,'公司乙'); assert.equal(s.batch.name,'B');
 s.edit('r1','remarks','保留'); assert.equal(await s.change_company('公司甲',async()=>false),false); assert.equal(s.batch.rows[0].remarks,'保留');
});
test('validate first saves all rows and version, then previews saved batch without financial activation', async () => {
 const calls=[]; const s=session(async(method,args)=>{ calls.push({method,args}); return method.endsWith('save_opening') ? envelope({batch:batch({version:'v2',rows:JSON.parse(args.payload).rows})}) : {fingerprint:'fp',errors:[],warnings:[],totals:{debit:'0.10',credit:'0',difference:'0.10'}}; });
 s.edit('r1','remarks','修改'); s.add_rows(2); await s.validate(); assert.equal(calls.length,2); assert.match(calls[0].method,/save_opening$/); assert.equal(calls[0].args.expected_version,'v1'); assert.equal(JSON.parse(calls[0].args.payload).rows.length,3); assert.match(calls[1].method,/preview_opening$/); assert.equal(s.preview.fingerprint,'fp'); assert.equal(s.dirty,false);
});
test('stale-version failures preserve every client value and busy requests cannot duplicate a save', async () => {
 let reject; let calls=0; const s=session(()=>{calls++;return new Promise((_resolve,fail)=>reject=fail);}); s.edit('r1','debit','888');
 const saving=s.save(); assert.equal(await s.save(),false); assert.equal(s.edit('r1','debit','999'),false); reject(new Error('版本冲突，请刷新核对')); assert.equal(await saving,false);
 assert.equal(calls,1); assert.equal(s.batch.rows[0].debit,'888'); assert.equal(s.dirty,true); assert.match(s.error,/版本冲突/); assert.equal(s.busy,false);
});
test('activation requires current server preview and permission; active batches are read-only', async () => {
 const calls=[]; const s=session(async(m,a)=>{calls.push({m,a}); return envelope({batch:batch({status:'Active'}),receipt:{name:'OB-1'}});});
 assert.equal(await s.activate(),false); assert.equal(calls.length,0); s.preview={fingerprint:'fp',errors:[]}; s.edit('r1','remarks','更改'); assert.equal(await s.activate(),false);
 s.dirty=false; s.preview={fingerprint:'fp',errors:[]}; assert.equal(await s.activate(),true); assert.equal(calls[0].a.fingerprint,'fp'); assert.equal(s.edit('r1','debit','2'),false); assert.equal(s.add_rows(1),0);
});
test('import and takeover previews do not write and preserve source rows', async () => {
 const calls=[]; const s=session(async(m,a)=>{calls.push({m,a});return {rows:[{account:'1001 - 库存现金',debit:0,credit:0}],errors:[]};});
 await s.preview_import({text:'a'}); assert.equal(calls.length,1); assert.match(calls[0].m,/preview_import$/); assert.equal(s.batch.rows.length,1); assert.equal(s.dirty,false); assert.equal(s.apply_import(),1); assert.equal(s.dirty,true);
 s.takeover({name:'JE-1',opening_date:'2025-12-31',source_names:['JE-1'],rows:[{account:'1001 - 库存现金',debit:0,credit:0,party:''}]}); assert.equal(s.batch.mode,'Takeover'); assert.deepEqual(s.batch.source_names,['JE-1']); assert.equal(s.batch.rows[0].debit,0); assert.equal(s.batch.rows[0].party,'');
});
test('grid view escapes all data and keeps batch errors separate from filtered totals', () => {
 const s=session(); s.batch.rows[0].remarks='<img src=x onerror=alert(1)>'; s.preview={errors:[{row_id:'r1',field:'remarks',message:'<script>'}]};
 const html=api.view(s); assert.ok(!html.includes('<img')); assert.ok(!html.includes('<script>')); assert.match(html,/&lt;img/); assert.match(html,/data-whole-totals/); assert.match(html,/data-filtered-totals/); assert.match(html,/data-horizontal-scroll/); assert.match(html,/aria-label=/);
});
test('layout fixes ordinary rows at 36px and one thin mouse horizontal range', () => {
 const css=fs.readFileSync(path.resolve(__dirname,'../public/css/china_opening_balance.css'),'utf8');
 assert.match(css,/height:\s*36px/); assert.match(css,/height:\s*30px/); assert.match(css,/height:\s*14px/); assert.match(css,/height:\s*6px/); assert.match(css,/height:\s*4px/); assert.match(css,/position:\s*sticky/);
});

test('combined historical takeover preserves all sources and validates one cutover date', () => {
 const s=session(); const candidates=[{name:'JE-1',opening_date:'2026-01-01',source_names:['JE-1'],rows:[{account:'1001 - 库存现金',debit:0,credit:0}]},{name:'JE-2',opening_date:'2026-01-01',source_names:['JE-2'],rows:[{account:'1122 - 应收账款',debit:0,credit:0}]}];
 assert.equal(s.takeover(candidates),true);assert.deepEqual(s.batch.source_names,['JE-1','JE-2']);assert.equal(s.batch.rows.length,2);assert.equal(s.batch.rows[0].debit,0);
});
test('foreign currency values never mix into company-currency batch totals', () => {
 const s=session(); s.batch.rows=[{debit:'10',credit:'0',account_currency:'USD',exchange_rate:'7.10',base_debit:'71.00',base_credit:'0'},{debit:'0',credit:'71.00',account_currency:'CNY'}];
 assert.deepEqual(api.totals(s.batch.rows,'CNY'),{debit:'71.00',credit:'71.00',difference:'0.00',invalid:0});
 assert.match(api.view(s),/借 71.00 · 贷 71.00 · 差额 0.00/);
});
test('missing rates show pending totals and changed rates recompute instead of using stale base values', () => {
 assert.deepEqual(api.totals([{debit:'10',credit:'0',account_currency:'USD',exchange_rate:'',base_debit:'71'}],'CNY'),{debit:'待计算',credit:'待计算',difference:'待计算',invalid:1});
 assert.equal(api.totals([{debit:'0.01',credit:'0',account_currency:'USD',exchange_rate:'7.5',base_debit:'999'}],'CNY').debit,'0.08');
 const s=session();s.preview={errors:[],totals:{debit:'99.99',credit:'99.99',difference:'0.00'}};assert.match(api.view(s),/借 99.99 · 贷 99.99/);
});
test('Excel thousands groups normalize only when well formed and malformed values remain validation errors', () => {
 assert.equal(api.to_minor('1,23'),null);assert.equal(api.to_minor('1234,567.89'),null);
 const p=api.payload(batch({rows:[{row_id:'r1',debit:'1,234.50',credit:'1,23',exchange_rate:'7.10'}]}));assert.equal(p.rows[0].debit,'1234.50');assert.equal(p.rows[0].credit,'1,23');
});
test('historical sources with different cutover dates cannot overwrite the current grid', () => {
 const s=session();const original=JSON.stringify(s.batch);assert.equal(s.takeover([{name:'A',opening_date:'2026-01-01',rows:[]},{name:'B',opening_date:'2026-02-01',rows:[]}]),false);assert.equal(JSON.stringify(s.batch),original);assert.match(s.error,/日期/);
});
test('foreign account selection and prefill require an explicit exchange rate; payload supplies calculated base amounts', () => {
 const s=session();s.accounts.push({name:'USD-1',account_number:'1002',account_currency:'USD'});s.edit('r1','account','USD-1');assert.equal(s.batch.rows[0].exchange_rate,'');
 s.edit('r1','debit','10');s.edit('r1','exchange_rate','7.10');const p=api.payload(s.batch);assert.equal(p.rows[0].base_debit,'71.00');assert.equal(p.rows[0].base_credit,'0.00');
 const other=session();other.accounts.push({name:'USD-2',account_currency:'USD'});other.prefill();assert.equal(other.batch.rows.find(r=>r.account==='USD-2').exchange_rate,'');
});
test('pasting a foreign currency without the optional rate leaves it pending', () => {
 const s=session();s.paste('1001 - 库存现金\t\t\tUSD\t10\t0\t外币','r1','account');assert.equal(s.batch.rows[0].exchange_rate,'');assert.equal(api.totals(s.batch.rows,'CNY').debit,'待计算');
});
test('loading another company clears a previous file import preview before the response', async () => {
 let resolve;const s=session(()=>new Promise(done=>resolve=done));s.import_preview={rows:[{account:'旧公司'}],errors:[]};const pending=s.load('公司乙');assert.equal(s.import_preview,null);resolve(envelope({company:'公司乙'}));await pending;
});
test('server amount and dimension errors mark the corresponding inputs accessibly', () => {
 const s=session();s.preview={errors:[{row_id:'r1',field:'amount',message:'金额错误'},{row_id:'r1',field:'cost_center',message:'成本中心错误'}]};const html=api.view(s);assert.match(html,/data-field="debit"[^>]*aria-invalid="true"/);assert.match(html,/data-field="dimension:cost_center"[^>]*aria-invalid="true"/);
});
test('safe integer cents remain exact when formatting a large batch total', () => {
 assert.equal(api.totals([{debit:'90071992547409.90',credit:'0'}]).debit,'90071992547409.90');
});
test('dimension columns use natural Chinese labels for standard accounting dimensions', () => {
 const html=api.view(session());assert.match(html,/>成本中心<\/th>/);assert.match(html,/第 1 行 成本中心/);
});
test('the custom Page route stays distinct from the Opening Balance DocType slug', () => {
 const pagePath=path.resolve(__dirname,'../china_finance/page/china_opening_balances');assert.ok(fs.existsSync(pagePath),'plural Page directory missing');const page=JSON.parse(fs.readFileSync(path.join(pagePath,'china_opening_balances.json'),'utf8'));assert.equal(page.name,'china-opening-balances');assert.equal(page.page_name,'china-opening-balances');assert.equal(page.title,'期初余额');assert.equal(fs.existsSync(path.resolve(pagePath,'../china_opening_balance')),false);
});
test('editing and draft save retain stale error rows for the current filter but require a new activation preview', async () => {
 const s=session(async()=>envelope());s.preview={fingerprint:'fp',errors:[{row_id:'r1',field:'remarks',message:'请核对'}],totals:{debit:'99.99',credit:'99.99',difference:'0.00'}};s.filters.errors=true;s.edit('r1','remarks','修改');assert.equal(s.visible_rows().length,1);assert.equal(s.preview.fingerprint,null);assert.equal(s.can_activate(),false);assert.match(api.view(s),/重新校验/);assert.ok(!api.view(s).includes('借 99.99'));
 await s.save();assert.equal(s.visible_rows().length,1);assert.equal(s.preview.stale,true);assert.equal(s.can_activate(),false);assert.ok(!api.view(s).includes('借 99.99'));
});
test('consecutive imports allocate unique stable business row IDs and edits target the second import', async () => {
 const s=session(async()=>({rows:[{row_id:'import-2',account:'1001 - 库存现金',debit:'10',credit:'0'}],errors:[]}));
 await s.preview_import({text:'first'});s.apply_import();const first=s.batch.rows.at(-1);await s.preview_import({text:'second'});s.apply_import();const second=s.batch.rows.at(-1);
 assert.notEqual(first.row_id,second.row_id);assert.notEqual(first.row_id,'import-2');assert.equal(second.import_source_row,'import-2');s.edit(second.row_id,'debit','20');assert.equal(first.debit,'10');assert.equal(second.debit,'20');
 const sent=api.payload(s.batch);assert.equal(new Set(sent.rows.map(r=>r.row_id)).size,sent.rows.length);assert.equal(sent.rows.at(-1).row_id,second.row_id);assert.equal(Object.hasOwn(sent.rows.at(-1),'import_source_row'),false);
});
test('cell focus memory survives a toolbar click and keeps the latest text selection', () => {
 const memory=new api.FocusMemory();memory.capture({dataset:{row:'r1',field:'remarks'},selectionStart:2,selectionEnd:4});memory.capture({dataset:{action:'save'}});assert.deepEqual(memory.value,{row:'r1',field:'remarks',start:2,end:4});memory.capture({dataset:{row:'r1',field:'remarks'},selectionStart:6,selectionEnd:6});memory.capture({});assert.equal(memory.value.start,6);
});
test('imported configured party types remain visible instead of silently showing an empty selection', () => {
 const s=session();s.batch.rows[0].party_type='Shareholder';s.batch.rows[0].party='股东甲';const html=api.view(s);assert.match(html,/<option value="Shareholder" selected>/);assert.match(html,/股东甲/);assert.equal(s.batch.rows[0].party_type,'Shareholder');
});
test('native Link initialization and detached controls cannot overwrite a newer pasted account', () => {
 const s=session();s.accounts.push({name:'100299 - 长名称银行',account_number:'100299',account_name:'长名称银行',account_currency:'CNY'});
 let options,displayed='',validationWrites=0,current=true;
 const control=api.create_link_control({make_control:config=>{options=config;return {set_input:value=>displayed=value,set_value:()=>validationWrites++,get_value:()=>displayed};},parent:{},fieldname:'account',doctype:'Account',label:'科目',value:s.batch.rows[0].account,is_current:()=>current,onchange:value=>s.edit('r1','account',value)});
 assert.equal(validationWrites,0,'read initialization must not start asynchronous native validation');assert.equal(displayed,'1001 - 库存现金');
 s.paste('100299 - 长名称银行\t\t\tCNY\t10\t0\t粘贴','r1','account');current=false;options.df.change();assert.equal(s.batch.rows[0].account,'100299 - 长名称银行');const html=api.view(s);assert.match(html,/ob-sticky-code">100299<\/td>/);assert.match(html,/ob-fullname">长名称银行<\/span>/);
 current=true;control.set_input('1001 - 库存现金');options.df.change();assert.equal(s.batch.rows[0].account,'1001 - 库存现金','current native user changes still reach the grid');
});
test('three-decimal envelope precision governs zero, balance, filters and saved base amounts', async () => {
 let sent;const s=new api.Session({call:async(_method,args)=>{sent=JSON.parse(args.payload);return envelope({precision:3,batch:batch({rows:sent.rows})});}});s.adopt(envelope({precision:3,batch:batch({rows:[{row_id:'a',account:'1001 - 库存现金',account_currency:'CNY',debit:'0.001',credit:'0'},{row_id:'b',account:'1001 - 库存现金',account_currency:'CNY',debit:'0.002',credit:'0.003'},{row_id:'z',account:'1001 - 库存现金',account_currency:'CNY',debit:'0.000',credit:'0.000'}]})}));
 assert.equal(s.precision,3);assert.equal(api.to_minor('0.001',3),1);assert.deepEqual(api.totals(s.batch.rows,'CNY',3),{debit:'0.003',credit:'0.003',difference:'0.000',invalid:0});s.filters.nonzero=true;assert.equal(s.visible_rows().length,2);assert.match(api.view(s),/借 0.003 · 贷 0.003 · 差额 0.000/);s.edit('a','remarks','三位');await s.save();assert.equal(sent.rows[0].base_debit,'0.001');assert.equal(sent.rows[0].base_credit,'0.000');assert.equal(s.precision,3);
});
test('zero and nine digit precisions are respected and half rounding gives the correct negative balance', () => {
 assert.deepEqual(api.totals([{debit:'12',credit:'12'}],'CNY',0),{debit:'12',credit:'12',difference:'0',invalid:0});assert.equal(api.to_minor('0.000000001',9),1);assert.equal(api.totals([{debit:'0.000000001',credit:'0'}],'CNY',9).debit,'0.000000001');assert.equal(api.totals([{debit:'0',credit:'0.001',account_currency:'USD',exchange_rate:'0.5'}],'CNY',3).difference,'-0.001');assert.equal(api.totals([{debit:'-0.001',credit:'0',account_currency:'USD',exchange_rate:'0.5'}],'CNY',3).debit,'-0.001');
});
test('the Page permits the same local finance user who can read and save an opening draft', () => {
 const page=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../china_finance/page/china_opening_balances/china_opening_balances.json'),'utf8'));const schema=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../china_finance/doctype/china_opening_balance/china_opening_balance.json'),'utf8'));assert.ok(schema.permissions.some(p=>p.role==='China Finance User' && p.read && p.write && p.create));assert.ok(page.roles.some(p=>p.role==='China Finance User'));
});
test('native JSON scientific notation preserves nine digit amounts and exchange rates exactly', () => {
 assert.equal(api.totals([{debit:1e-9,credit:0,account_currency:'CNY'}],'CNY',9).debit,'0.000000001');assert.equal(api.totals([{debit:1,credit:0,account_currency:'USD',exchange_rate:1e-9}],'CNY',9).debit,'0.000000001');assert.equal(api.to_minor('1e3',0),1000);assert.equal(api.to_minor('1e-4',3),null);
});
