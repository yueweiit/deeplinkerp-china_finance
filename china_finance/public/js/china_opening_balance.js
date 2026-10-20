/* One opening-balance grid is shared by Desk and read-only fixture previews. */
(function (root, factory) {
 const api = factory(root);
 if (typeof module === 'object' && module.exports) module.exports = api;
 root.china_finance = root.china_finance || {};
 root.china_finance.opening_balance = api;
})(typeof window === 'undefined' ? globalThis : window, function (root) {
 'use strict';
 const __ = (value, args = []) => typeof root.__ === 'function' ? root.__(value,args) : value.replace(/\{(\d+)\}/g, (m,i) => args[i] ?? m);
 const METHOD = 'china_finance.services.opening_balance.';
 const clone = value => JSON.parse(JSON.stringify(value));
 const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const fields = ['account','party_type','party','account_currency','debit','credit','remarks','exchange_rate','base_debit','base_credit'];
 let next_id = 0;
 const row_id = () => `local-${Date.now().toString(36)}-${++next_id}`;
 function new_row(value = {}, currency = '') { return {account:'',party_type:'',party:'',account_currency:currency,exchange_rate:value.account_currency && value.account_currency!==currency?'':1,debit:'0',credit:'0',remarks:'',dimensions_json:{},...clone(value),row_id:value.row_id || row_id()}; }
 const decimal_precision = value => value!==null && value!=='' && value!==undefined && Number.isInteger(Number(value)) && Number(value)>=0 && Number(value)<=9 ? Number(value) : 2;
 function scaled_amount(value,precision=2) {
  precision=decimal_precision(precision);
  const text=String(value ?? '0').trim();if(!/^-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(text))return null;
  const match=text.replace(/,/g,'').match(/^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/),exponent=Number(match[4] || 0);if(!Number.isSafeInteger(exponent) || Math.abs(exponent)>308)return null;
  const coefficient=BigInt(match[2]+(match[3] || '')),shift=precision+exponent-(match[3] || '').length;
  let result;if(shift>=0)result=coefficient*(10n ** BigInt(shift));else{const divisor=10n ** BigInt(-shift);if(coefficient%divisor!==0n)return null;result=coefficient/divisor;}
  return match[1]?-result:result;
 }
 function to_minor(value,precision=2) {const scaled=scaled_amount(value,precision);if(scaled===null)return null;const result=Number(scaled);return Number.isSafeInteger(result)?result:null;}
 const amount = (n,precision=2) => {precision=decimal_precision(precision);const minor=BigInt(n),digits=(minor<0n?-minor:minor).toString().padStart(precision+1,'0');return (minor<0n?'-':'')+(precision?digits.slice(0,-precision)+'.'+digits.slice(-precision):digits);};
 function base_minor(row,field,currency,precision=2) {
  const minor=scaled_amount(row[field],precision);if(minor===null)return null;
  if(!currency || !row.account_currency || row.account_currency===currency)return minor;
  const numerator=scaled_amount(row.exchange_rate,9),scale=1000000000n;if(numerator===null || numerator<=0n)return null;
  const result=(minor<0n?-minor:minor)*numerator,rounded=(result+scale/2n)/scale;return minor<0n?-rounded:rounded;
 }
 function totals(rows,currency='',precision=2) {
  let debit=0n,credit=0n,invalid=0;
  rows.forEach(row => { const d=base_minor(row,'debit',currency,precision),c=base_minor(row,'credit',currency,precision); if (d===null || c===null) invalid++; debit+=d || 0n; credit+=c || 0n; });
  return invalid?{debit:__('待计算'),credit:__('待计算'),difference:__('待计算'),invalid}:{debit:amount(debit,precision),credit:amount(credit,precision),difference:amount(debit-credit,precision),invalid};
 }
 function parse_tsv(text) {
  const rows=[]; let row=[],value='',quoted=false;
  const input=String(text ?? '');
  for (let i=0;i<input.length;i++) {
   const c=input[i];
   if (c==='"' && (quoted || value==='')) { if (quoted && input[i+1]==='"') { value+='"';i++; } else quoted=!quoted; }
   else if (!quoted && (c==='\t' || c==='\n' || c==='\r')) { row.push(value); value=''; if (c!=='\t') { rows.push(row);row=[];if(c==='\r' && input[i+1]==='\n')i++; } }
   else value+=c;
  }
  row.push(value); if (row.some(cell=>cell!=='') || !rows.length) rows.push(row);
  return rows;
 }
 function payload(batch,precision=2) {
  const out={}; ['name','company','opening_date','currency','mode','source_names','previous_batch','root_batch'].forEach(key=> {if(batch[key]!==undefined)out[key]=clone(batch[key]);});
  out.rows=(batch.rows || []).map(row=> {const out={row_id:row.row_id}; [...fields,'dimensions_json'].forEach(key=>{if(row[key]!==undefined)out[key]=['debit','credit','base_debit','base_credit'].includes(key) && scaled_amount(row[key],precision)!==null ? String(row[key]).replace(/,/g,'') : clone(row[key]);});for(const field of ['debit','credit']){const base=base_minor(row,field,batch.currency,precision);if(base!==null)out['base_'+field]=amount(base,precision);}return out;}); return out;
 }
 class Session {
  constructor({call,onchange=()=>{}}) { this.call=call;this.onchange=onchange;this.company='';this.precision=2;this.batch=null;this.accounts=[];this.dimensions=[];this.history_candidates=[];this.permissions={};this.filters={nonzero:false,errors:false,search:''};this.selected=new Set();this.dirty=false;this.busy=false;this.loading=false;this.request=0;this.revision=0;this.preview=null;this.import_preview=null;this.error=''; }
  adopt(data) {
   this.company=data.company || data.batch?.company || this.company;this.currency=data.currency || data.batch?.currency || '';this.precision=decimal_precision(data.precision);this.opening_date=data.opening_date || '';
   this.accounts=clone(data.accounts || []);this.dimensions=clone(data.dimensions || []);this.history_candidates=clone(data.history_candidates || []);this.permissions=clone(data.permissions || {});
   this.batch=data.batch ? clone(data.batch) : {name:'',company:this.company,currency:this.currency,opening_date:this.opening_date,mode:'New',status:'Draft',version:null,rows:[]};
   this.batch.rows=(this.batch.rows || []).map(row=>new_row(row,this.currency));this.dirty=false;this.preview=null;this.import_preview=null;this.selected.clear();this.error='';this.revision++;
  }
  editable() { return !!this.company && this.batch?.status==='Draft' && !!this.permissions.write && !this.busy && !this.loading; }
  changed() { this.dirty=true;if(this.preview)this.preview={...this.preview,fingerprint:null,stale:true};this.error='';this.revision++;this.onchange('edit'); }
  edit(id,field,value) {
   if(!this.editable())return false; const row=this.batch.rows.find(row=>row.row_id===id);if(!row)return false;
   if(field.startsWith('dimension:')) {const key=field.slice(10);let dims=typeof row.dimensions_json==='string' ? JSON.parse(row.dimensions_json || '{}') : {...row.dimensions_json};if(dims[key]===value)return false;dims[key]=value;row.dimensions_json=dims;}
   else {if(!fields.includes(field) || row[field]===value)return false;row[field]=value;if(field==='party_type')row.party='';if(field==='account'){const account=this.accounts.find(a=>a.name===value || a.account_number===value);if(account){row.account=account.name;const currency=account.account_currency || this.currency;if(row.account_currency!==currency)row.exchange_rate=currency===this.currency?'1':'';row.account_currency=currency;}}}
   this.changed();return true;
  }
  set_header(field,value) {if(!this.editable() || !['opening_date','currency'].includes(field) || this.batch[field]===value)return false;this.batch[field]=value;this.changed();return true;}
  add_rows(count=1) {if(!this.editable())return 0;const n=Math.max(1,Math.min(500,Number(count)||1));for(let i=0;i<n;i++)this.batch.rows.push(new_row({},this.batch.currency));this.changed();return n;}
  prefill() {if(!this.editable())return 0;const used=new Set(this.batch.rows.map(row=>row.account));const rows=this.accounts.filter(a=>!used.has(a.name)).map(a=>new_row({account:a.name,account_currency:a.account_currency || this.currency},this.currency));if(rows.length){this.batch.rows.push(...rows);this.changed();}return rows.length;}
  delete_selected() {if(!this.editable())return 0;const before=this.batch.rows.length;this.batch.rows=this.batch.rows.filter(row=>!this.selected.has(row.row_id));const count=before-this.batch.rows.length;if(count){this.selected.clear();this.changed();}return count;}
  visible_rows() {const errors=new Set((this.preview?.errors || []).map(error=>error.row_id));const search=this.filters.search.toLowerCase();return (this.batch?.rows || []).filter(row=>(!this.filters.nonzero || scaled_amount(row.debit,this.precision)!==0n || scaled_amount(row.credit,this.precision)!==0n) && (!this.filters.errors || errors.has(row.row_id)) && (!search || [row.account,row.party,row.remarks].some(value=>String(value || '').toLowerCase().includes(search))));}
  paste(text,id,field) {
   if(!this.editable())return 0;const start=this.batch.rows.findIndex(row=>row.row_id===id),column=fields.indexOf(field);if(start<0 || column<0)return 0;
   const rows=parse_tsv(text);if(rows.length>500)throw new Error(__('一次最多粘贴 500 行，请分批粘贴。'));
   rows.forEach((values,offset)=>{if(!this.batch.rows[start+offset])this.batch.rows.push(new_row({},this.currency));const row=this.batch.rows[start+offset],old_currency=row.account_currency;values.forEach((value,index)=>{const key=fields[column+index];if(key){row[key]=value;}});const account=this.accounts.find(a=>a.name===row.account || a.account_number===row.account);if(account)row.account=account.name;if(row.account_currency!==old_currency && !values.some((_v,i)=>fields[column+i]==='exchange_rate'))row.exchange_rate=row.account_currency===this.currency?'1':'';});
   this.changed();return rows.length;
  }
  async load(company=this.company) {
   if(this.busy || this.dirty || !company)return false; const token=++this.request,revision=this.revision;this.company=company;this.loading=true;this.batch=null;this.preview=null;this.import_preview=null;this.selected.clear();this.error='';this.onchange('loading');
   try {const data=await this.call(METHOD+'get_opening',{company});if(token!==this.request || revision!==this.revision || company!==this.company)return false;this.adopt(data);this.onchange('loaded');return true;}
   catch(error){if(token===this.request){this.error=error?.message || __('读取失败，请重试');this.onchange('error');}return false;}
   finally{if(token===this.request){this.loading=false;this.onchange('loaded');}}
  }
  async change_company(company,confirm_discard) {if(this.busy || company===this.company)return false;if(this.dirty && !(await confirm_discard()))return false;this.dirty=false;this.revision++;this.request++;return this.load(company);}
  async run(action) {if(this.busy || this.loading)return false;this.busy=true;this.error='';this.onchange('busy');try{return await action();}catch(error){this.error=error?.message || __('请求失败，编辑内容已保留，请核对后重试');return false;}finally{this.busy=false;this.onchange('processed');}}
  async save_current() {const previous_preview=this.preview;const data=await this.call(METHOD+'save_opening',{payload:JSON.stringify(payload(this.batch,this.precision)),expected_version:this.batch.version ?? null});if(!data?.batch)throw new Error(__('服务器未返回已保存批次，编辑内容已保留'));this.adopt({...data,precision:data.precision ?? this.precision,accounts:data.accounts || this.accounts,dimensions:data.dimensions || this.dimensions,history_candidates:data.history_candidates || this.history_candidates,permissions:data.permissions || this.permissions});if(previous_preview)this.preview={...previous_preview,fingerprint:null,stale:true};return true;}
  async save() {if(!this.editable())return false;return this.run(()=>this.save_current());}
  async validate() {if(!this.editable())return false;return this.run(async()=>{if(this.dirty || !this.batch.name)await this.save_current();this.preview=await this.call(METHOD+'preview_opening',{name:this.batch.name});return true;});}
  can_activate() {return this.batch?.status==='Draft' && this.permissions.activate && !this.dirty && !!this.preview?.fingerprint && !(this.preview.errors || []).length;}
  async activate() {if(!this.can_activate())return false;return this.run(async()=>{const data=await this.call(METHOD+'activate_opening',{name:this.batch.name,fingerprint:this.preview.fingerprint});if(!data?.batch)throw new Error(__('服务器未返回启用结果，请刷新核对'));this.receipt=data.receipt;this.adopt({...data,precision:data.precision ?? this.precision,accounts:data.accounts || this.accounts,dimensions:data.dimensions || this.dimensions,permissions:data.permissions || this.permissions});return true;});}
  async start_adjustment() {if(this.batch?.status!=='Active' || !this.permissions.write)return false;return this.run(async()=>{const data=await this.call(METHOD+'start_adjustment',{name:this.batch.name,expected_version:this.batch.version});this.adopt({...data,precision:data.precision ?? this.precision,accounts:data.accounts || this.accounts,dimensions:data.dimensions || this.dimensions,permissions:data.permissions || this.permissions});return true;});}
  async preview_import(args) {if(!this.editable())return false;return this.run(async()=>{this.import_preview=await this.call(METHOD+'preview_import',{company:this.company,...args});return true;});}
  apply_import() {if(!this.editable() || !this.import_preview || this.import_preview.errors?.length)return 0;const rows=this.import_preview.rows || [],used=new Set(this.batch.rows.map(row=>row.row_id));this.batch.rows.push(...rows.map(row=>{let id=row_id();while(used.has(id))id=row_id();used.add(id);return new_row({...row,import_source_row:row.import_source_row ?? row.row_id,row_id:id},this.currency);}));this.import_preview=null;if(rows.length)this.changed();return rows.length;}
  takeover(candidate) {if(!this.editable())return false;const candidates=Array.isArray(candidate)?candidate:[candidate];if(!candidates.length)return false;if(new Set(candidates.map(c=>c.opening_date)).size>1){this.error=__('历史期初切换日期不同，请分别核对来源。');this.onchange('error');return false;}this.batch.mode='Takeover';this.batch.opening_date=candidates[0].opening_date || this.batch.opening_date;this.batch.source_names=[...new Set(candidates.flatMap(c=>c.source_names || [c.name]))];this.batch.rows=candidates.flatMap(c=>c.rows || []).map(row=>new_row(row,this.currency));this.changed();return true;}
 }
 const labels={account:__('科目全称'),party_type:__('往来类型'),party:__('往来单位'),account_currency:__('币种'),debit:__('借方期初'),credit:__('贷方期初'),remarks:__('备注'),exchange_rate:__('汇率'),base_debit:__('本位币借方'),base_credit:__('本位币贷方')};
 const disabled = value => value ? ' disabled' : '';
 const dimension_definition = value => {const field=typeof value==='string'?{fieldname:value}:value;const native=root.frappe?.meta?.get_docfield?.('Journal Entry Account',field.fieldname) || {};return {...native,...field,label:__(field.label || native.label || ({cost_center:'成本中心',project:'项目'}[field.fieldname]) || field.fieldname),options:field.options || native.options};};
 function view(s,options={}) {
  const batch=s.batch,locked=!s.editable(),rows=s.visible_rows(),errors=s.preview?.errors || [],computed=totals(batch?.rows || [],batch?.currency,s.precision),whole=s.preview?.totals && !s.preview.stale && !s.dirty ? {...computed,...s.preview.totals} : computed,filtered=totals(rows,batch?.currency,s.precision),show_fx=!!options.fx;
  const button=(action,text,off=false,primary=false)=>`<button type="button" class="btn ${primary?'btn-primary':'btn-default'}" data-action="${action}"${disabled(off || s.busy || s.loading)}>${escape(text)}</button>`;
  const dims=s.dimensions.map(dimension_definition);
  const columns=['account','party_type','party','account_currency','debit','credit','remarks',...(show_fx?['exchange_rate','base_debit','base_credit']:[]),...dims.map(d=>'dimension:'+d.fieldname)];
  function cell(row,field,index) {
   const dimension=field.startsWith('dimension:'),dimension_key=field.slice(10),label=dimension ? dims.find(d=>d.fieldname===dimension_key)?.label || dimension_key : labels[field];
   let dims_value={};try{dims_value=typeof row.dimensions_json==='string'?JSON.parse(row.dimensions_json || '{}'):row.dimensions_json || {};}catch(_e){ /* Invalid source JSON is reported by server validation. */ }
   let value=dimension?dims_value[dimension_key] || '':row[field] ?? '';if(['base_debit','base_credit'].includes(field)){const computed=base_minor(row,field.slice(5),batch.currency,s.precision);value=computed===null?__('待计算'):amount(computed,s.precision);}
   const issues=errors.filter(e=>e.row_id===row.row_id && (!e.field || e.field===field || e.field===dimension_key || (e.field==='amount' && ['debit','credit','exchange_rate','base_debit','base_credit'].includes(field))));
   const attrs=`data-row="${escape(row.row_id)}" data-field="${escape(field)}" aria-label="${escape(__('第 {0} 行 {1}',[index+1,label]))}"${issues.length?' aria-invalid="true"':''} title="${escape(issues.map(e=>e.message).join('；') || value)}"${disabled(locked)}`;
   const numeric=['debit','credit','exchange_rate','base_debit','base_credit'].includes(field);
   let input;
   if(field==='party_type')input=`<select ${attrs}><option value=""></option>${value && !['Customer','Supplier','Employee'].includes(value)?`<option value="${escape(value)}" selected>${escape(value==='Shareholder'?__('股东'):value)}</option>`:''}${[['Customer',__('客户')],['Supplier',__('供应商')],['Employee',__('员工')]].map(([v,l])=>`<option value="${v}"${value===v?' selected':''}>${escape(l)}</option>`).join('')}</select>`;
   else input=`<input type="text" ${attrs} value="${escape(value)}"${numeric?' inputmode="decimal" class="ob-number"':''}${field==='account'||field==='party'||dimension?' data-native-link="1"':''}${['base_debit','base_credit'].includes(field)?' readonly tabindex="-1"':''} autocomplete="off">`;
   const account=s.accounts.find(a=>a.name===row.account);
   return `<td class="${field==='account'?'ob-account ob-sticky-account':''} ${numeric?'ob-number':''} ${issues.length?'ob-cell-error':''}"><div class="ob-cell-host">${input}${field==='account'?`<span class="ob-fullname">${escape(account?.account_name || row.account || '')}</span>`:''}</div></td>`;
  }
  return `<div class="ob-toolbar"><label class="ob-company"><span>${escape(__('公司'))}</span><input type="text" data-company value="${escape(s.company)}" aria-label="${escape(__('公司'))}" autocomplete="off"${disabled(s.busy || s.loading)}></label><label><span>${escape(__('切换日期'))}</span><input type="date" data-header="opening_date" value="${escape(batch?.opening_date || '')}" aria-label="${escape(__('切换日期'))}"${disabled(locked)}></label><span class="ob-currency">${escape(batch?.currency || s.currency || '—')}</span><span class="ob-state">${escape(s.loading?__('正在读取…'):batch?.status==='Active'?__('已启用 · 只读'):s.dirty?__('草稿 · 未保存'):__('草稿'))}</span><div class="ob-primary-actions">${button('save',__('保存草稿'),locked || !s.dirty,true)}${button('validate',__('校验整表'),locked)}${batch?.status==='Active'?button('adjust',__('创建调整'),!s.permissions.write):button('activate',batch?.mode==='Takeover'?__('接管整批'):__('启用整批'),!s.can_activate())}</div></div>
  <div class="ob-tools">${button('prefill',__('预填科目'),locked)}${button('add',__('添加空行'),locked)}${button('delete',__('删除所选'),locked || !s.selected.size)}${button('import',__('导入预览'),locked)}${button('template',__('下载模板'),false)}${button('refresh',__('刷新'),false)}${s.history_candidates.length?`<select data-history aria-label="${escape(__('载入历史期初'))}"${disabled(locked)}><option value="">${escape(__('历史期初接管…'))}</option><option value="all">${escape(__('载入全部历史期初'))}</option>${s.history_candidates.map((c,i)=>`<option value="${i}">${escape(c.label || c.name)}</option>`).join('')}</select>`:''}<label class="ob-check"><input type="checkbox" data-filter="nonzero"${s.filters.nonzero?' checked':''}>${escape(__('仅非零'))}</label><label class="ob-check"><input type="checkbox" data-filter="errors"${s.filters.errors?' checked':''}>${escape(__('仅错误'))}</label><label class="ob-check"><input type="checkbox" data-fx${show_fx?' checked':''}>${escape(__('外币列'))}</label><input class="ob-search" data-filter="search" value="${escape(s.filters.search)}" aria-label="${escape(__('筛选科目、往来或备注'))}" placeholder="${escape(__('搜索科目 / 往来 / 备注'))}"></div>
  ${s.error?`<div class="ob-alert" role="alert">${escape(s.error)}</div>`:''}
  ${s.import_preview?`<div class="ob-import-preview" role="region" aria-label="${escape(__('导入预览'))}"><strong>${escape(__('导入预览：{0} 行',[s.import_preview.rows?.length || 0]))}</strong> ${escape((s.import_preview.errors || []).map(e=>e.message || String(e)).join('；'))}${button('apply-import',__('批量应用到表格'),!!s.import_preview.errors?.length)}${button('cancel-import',__('取消预览'))}<div>${(s.import_preview.rows || []).slice(0,5).map(r=>`<span>${escape(r.account)} · ${escape(r.debit ?? 0)} / ${escape(r.credit ?? 0)}</span>`).join('')}</div></div>`:''}
  <div class="ob-grid-scroll" tabindex="-1"><table class="ob-grid"><colgroup><col style="width:30px"><col style="width:40px"><col style="width:84px"><col style="width:270px"><col style="width:90px"><col style="width:180px"><col style="width:70px"><col style="width:120px"><col style="width:120px"><col style="width:180px">${show_fx?'<col style="width:90px"><col style="width:120px"><col style="width:120px">':''}${dims.map(()=>'<col style="width:150px">').join('')}</colgroup><thead><tr><th class="ob-sticky-check"><input type="checkbox" data-select-all aria-label="${escape(__('选择可见行'))}"${disabled(locked)}></th><th class="ob-sticky-index">#</th><th class="ob-sticky-code">${escape(__('科目编码'))}</th>${columns.map(field=>`<th class="${field==='account'?'ob-sticky-account':''}">${escape(field.startsWith('dimension:')?dims.find(d=>d.fieldname===field.slice(10))?.label || field.slice(10):labels[field])}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>{const i=batch.rows.indexOf(row);const account=s.accounts.find(a=>a.name===row.account);return `<tr data-grid-row="${escape(row.row_id)}"${s.selected.has(row.row_id)?' class="is-selected"':''}><td class="ob-sticky-check"><input type="checkbox" data-select="${escape(row.row_id)}" aria-label="${escape(__('选择第 {0} 行',[i+1]))}"${s.selected.has(row.row_id)?' checked':''}${disabled(locked)}></td><td class="ob-sticky-index">${i+1}</td><td class="ob-sticky-code">${escape(account?.account_number || '')}</td>${columns.map(field=>cell(row,field,i)).join('')}</tr>`;}).join('') || `<tr><td colspan="${columns.length+3}" class="ob-empty">${escape(s.loading?__('正在读取期初余额…'):s.company?__('点击预填科目、添加空行，或从 Excel 粘贴。'):__('请先选择公司。'))}</td></tr>`}</tbody></table></div>
  <div class="ob-scroll-dock"><input type="range" data-horizontal-scroll aria-label="${escape(__('水平滚动期初余额表'))}" min="0" max="0" value="0" step="1"></div>
  <div class="ob-footer"><strong data-whole-totals><span>${escape(batch?.currency || s.currency || '')}</span> ${escape(__('整批 {0} 行 · 借 {1} · 贷 {2} · 差额 {3} · 错误 {4}',[batch?.rows.length || 0,whole.debit,whole.credit,whole.difference,s.preview?errors.length:whole.invalid]))}</strong><span data-filtered-totals>${escape(__('当前显示 {0} 行 · 借 {1} · 贷 {2}',[rows.length,filtered.debit,filtered.credit]))}</span></div>
  ${s.preview?`<div class="ob-validation" role="status">${escape(s.preview.stale?__('内容已修改，请重新校验；以下为上次校验结果。'):errors.length?__('整表校验发现 {0} 个错误',[errors.length]):__('整表校验已完成'))}${errors.slice(0,5).map(e=>`<button type="button" data-error-row="${escape(e.row_id || '')}">${escape(e.message)}</button>`).join('')}${(s.preview.warnings || []).slice(0,5).map(w=>`<span>${escape(w.message || String(w))}</span>`).join('')}</div>`:''}`;
 }
 function create_link_control({make_control,parent,fieldname,doctype,label,get_query,value,is_current,onchange}) {
  let initializing=true;
  const control=make_control({parent,df:{fieldname,fieldtype:'Link',options:doctype,label,only_select:true,get_query,change:()=>{if(!initializing && is_current())onchange(control.get_value());}},render_input:true});
  // set_value validates asynchronously and fires change; this is a read-only
  // display initialization of an already loaded value, not a new user edit.
  control.set_input(value);initializing=false;return control;
 }
 class FocusMemory {
  constructor(){this.value=null;}
  capture(target){if(target?.dataset?.row && target.dataset.field)this.value={row:target.dataset.row,field:target.dataset.field,start:target.selectionStart,end:target.selectionEnd};return this.value;}
 }
 function mount(element,options={}) {
  if(element.opening_balance)return element.opening_balance;
  const doc=element.ownerDocument,frappe=options.frappe || root.frappe;
  const s=new Session({call:options.call || ((method,args)=>frappe.xcall(method,args)),onchange:event=>{if(event==='edit')update_feedback();else render();}});
  let fx=false,restoring=false,navigating=false;const focus_memory=new FocusMemory();
  element.classList.add('china-opening-balance');
  const editor={session:s,render,load:company=>s.load(company),destroy:()=>{root.removeEventListener?.('resize',layout);observer?.disconnect();},confirm_discard};element.opening_balance=editor;
  function snapshot() {const scroll=element.querySelector('.ob-grid-scroll');return {left:scroll?.scrollLeft || 0,top:scroll?.scrollTop || 0,...focus_memory.capture(doc.activeElement)};}
  function render() {
   const state=snapshot();element.innerHTML=view(s,{fx});layout();
   const scroll=element.querySelector('.ob-grid-scroll');if(scroll){scroll.scrollLeft=state.left;scroll.scrollTop=state.top;scroll.addEventListener('scroll',sync_scroll,{passive:true});}
   const focus=[...element.querySelectorAll('[data-row][data-field]')].find(input=>input.dataset.row===state.row && input.dataset.field===state.field);
   if(focus && !focus.disabled){restoring=true;focus.focus({preventScroll:true});try{focus.setSelectionRange(state.start,state.end);}catch(_e){ /* Invalid source JSON is reported by server validation. */ }restoring=false;}
   sync_scroll();
  }
  function update_feedback() {
   const whole=totals(s.batch?.rows || [],s.batch?.currency,s.precision),filtered=totals(s.visible_rows(),s.batch?.currency,s.precision);
   const summary=element.querySelector('[data-whole-totals]');if(summary)summary.textContent=(s.batch?.currency || '')+' '+__('整批 {0} 行 · 借 {1} · 贷 {2} · 差额 {3} · 错误 {4}',[s.batch?.rows.length || 0,whole.debit,whole.credit,whole.difference,s.preview?s.preview.errors?.length || 0:whole.invalid]);
   const subset=element.querySelector('[data-filtered-totals]');if(subset)subset.textContent=__('当前显示 {0} 行 · 借 {1} · 贷 {2}',[s.visible_rows().length,filtered.debit,filtered.credit]);
   element.querySelectorAll('[data-grid-row]').forEach(tr=>{const row=s.batch?.rows.find(row=>row.row_id===tr.dataset.gridRow);if(!row)return;['account_currency','exchange_rate','base_debit','base_credit'].forEach(field=>{const input=tr.querySelector(`[data-field="${field}"]`);if(input && input!==doc.activeElement){const base=field.startsWith('base_')?base_minor(row,field.slice(5),s.batch.currency,s.precision):null;input.value=field.startsWith('base_')?(base===null?__('待计算'):amount(base,s.precision)):row[field] ?? '';}});const account=s.accounts.find(a=>a.name===row.account);const code=tr.querySelector('.ob-sticky-code');if(code)code.textContent=account?.account_number || '';const fullname=tr.querySelector('.ob-fullname');if(fullname)fullname.textContent=account?.account_name || row.account || '';});
   const status=element.querySelector('.ob-state');if(status)status.textContent=__('草稿 · 未保存');
   const save=element.querySelector('[data-action="save"]');if(save)save.disabled=!s.editable() || !s.dirty;
   const activate=element.querySelector('[data-action="activate"]');if(activate)activate.disabled=true;
   const validation=element.querySelector('.ob-validation');if(validation)validation.textContent=__('内容已修改，请重新校验；当前显示上次校验标记。');
  }
  function layout() {
   const scroll=element.querySelector('.ob-grid-scroll');if(!scroll)return;
   const rect=scroll.getBoundingClientRect();const bottom=(element.querySelector('.ob-footer')?.offsetHeight || 36)+(element.querySelector('.ob-scroll-dock')?.offsetHeight || 14)+(element.querySelector('.ob-validation')?.offsetHeight || 0)+16;
   scroll.style.height=`${Math.max(160,(root.innerHeight || 768)-rect.top-bottom)}px`;sync_scroll();
  }
  function sync_scroll() {const scroll=element.querySelector('.ob-grid-scroll'),range=element.querySelector('[data-horizontal-scroll]');if(!scroll || !range)return;range.max=String(Math.max(0,scroll.scrollWidth-scroll.clientWidth));range.value=String(scroll.scrollLeft);range.disabled=Number(range.max)===0;range.style.setProperty('--ob-thumb-width',`${Math.max(32,range.clientWidth*scroll.clientWidth/Math.max(1,scroll.scrollWidth))}px`);}
  const confirm_message=message=>new Promise(resolve=>{if(frappe?.confirm)frappe.confirm(escape(message).replace(/\n/g,'<br>'),()=>resolve(true),()=>resolve(false));else resolve(root.confirm?.(message) || false);});
  async function confirm_discard() {if(s.busy)return false;if(!s.dirty)return true;return confirm_message(__('尚有未保存的期初余额，离开将放弃这些修改。是否继续？'));}
  function notify(message) {s.error=message;render();}
  function native_link(input) {
   if(restoring || !frappe?.ui?.form?.make_control || input.dataset.linkReady || input.disabled)return;
   const field=input.dataset.field,company=!!input.dataset.company;
   const row=s.batch?.rows.find(row=>row.row_id===input.dataset.row);
   let doctype=company?'Company':field==='account'?'Account':field==='party'?row?.party_type:field?.startsWith('dimension:')?dimension_definition(s.dimensions.find(d=>(typeof d==='string'?d:d.fieldname)===field.slice(10)) || field.slice(10)).options:null;
   if(!doctype)return;
   const host=company?input.parentElement:input.closest('.ob-cell-host'),original=input.value,attrs={...input.dataset};
   const parent=doc.createElement('div');parent.className='ob-native-link';host.insertBefore(parent,input);input.hidden=true;
   const control=create_link_control({make_control:config=>frappe.ui.form.make_control(config),parent,fieldname:company?'company':field,doctype,label:input.getAttribute('aria-label'),value:original,is_current:()=>parent.isConnected && element.contains(parent),get_query:()=>({filters:doctype==='Account'?{company:s.company,is_group:0,disabled:0,report_type:'Balance Sheet'}:doctype==='Company'?{is_group:0}:doctype==='Customer'||doctype==='Supplier'?{disabled:0}:{}}),onchange:value=>{if(company){change_company(value);}else{s.edit(attrs.row,field,value);if(field==='account'){const account=s.accounts.find(a=>a.name===value);const name=host.querySelector('.ob-fullname');if(name)name.textContent=account?.account_name || value;}}}});
   const native=control.$input?.get?.(0);if(native){Object.entries(attrs).forEach(([key,value])=>native.dataset[key]=value);native.dataset.linkReady='1';native.setAttribute('aria-label',input.getAttribute('aria-label'));native.focus({preventScroll:true});}
   input.remove();
  }
  async function change_company(company) {const before=s.company;if(!company || company===before)return;const ok=await s.change_company(company,confirm_discard);if(!ok && s.company===before){render();return;}options.on_company?.(s.company);}
  element.addEventListener('focusin',event=>{const input=event.target;focus_memory.capture(input);if(input.matches('[data-native-link],[data-company]'))native_link(input);});
  element.addEventListener('input',event=>{
   const input=event.target;focus_memory.capture(input);if(input.matches('[data-row][data-field]') && input.tagName!=='SELECT')s.edit(input.dataset.row,input.dataset.field,input.value);
   else if(input.matches('[data-horizontal-scroll]')){const scroll=element.querySelector('.ob-grid-scroll');if(scroll)scroll.scrollLeft=Number(input.value);}
   else if(input.dataset.filter==='search'){const state=snapshot();s.filters.search=input.value;render();const search=element.querySelector('[data-filter="search"]');search.focus();search.setSelectionRange(input.selectionStart,input.selectionEnd);const scroll=element.querySelector('.ob-grid-scroll');scroll.scrollTop=state.top;}
  });
  element.addEventListener('change',async event=>{
   const input=event.target;
   if(input.matches('[data-row][data-field]')){const changed=s.edit(input.dataset.row,input.dataset.field,input.value);if((changed && ['account','party_type'].includes(input.dataset.field)) || s.filters.nonzero || s.filters.errors)render();}
   else if(input.matches('[data-company]') && !input.dataset.linkReady)await change_company(input.value);
   else if(input.dataset.header)s.set_header(input.dataset.header,input.value);
   else if(input.dataset.filter && input.dataset.filter!=='search'){s.filters[input.dataset.filter]=input.checked;render();}
   else if(input.hasAttribute('data-fx')){fx=input.checked;render();}
   else if(input.dataset.select){input.checked?s.selected.add(input.dataset.select):s.selected.delete(input.dataset.select);render();}
   else if(input.hasAttribute('data-select-all')){s.visible_rows().forEach(row=>input.checked?s.selected.add(row.row_id):s.selected.delete(row.row_id));render();}
   else if(input.hasAttribute('data-history') && input.value!==''){const candidate=input.value==='all'?s.history_candidates:s.history_candidates[Number(input.value)];if((!s.dirty || await confirm_discard()) && candidate){s.takeover(candidate);render();}}
  });
  element.addEventListener('paste',event=>{
   const input=event.target;if(!input.matches('[data-row][data-field]'))return;
   const text=event.clipboardData?.getData('text/plain') || '';if(!/[\t\n\r]/.test(text))return;event.preventDefault();try{s.paste(text,input.dataset.row,input.dataset.field);render();}catch(error){notify(error.message);}
  });
  element.addEventListener('select',event=>focus_memory.capture(event.target));
  element.addEventListener('keyup',event=>focus_memory.capture(event.target));
  element.addEventListener('keydown',event=>{
   const input=event.target;
   if(event.key==='Tab' && input.matches('[data-row][data-field]')){const controls=[...element.querySelectorAll('[data-row][data-field]:not(:disabled):not([readonly]):not([hidden])')],index=controls.indexOf(input),next=controls[index+(event.shiftKey?-1:1)];if(next){event.preventDefault();next.focus();}else if(!event.shiftKey && s.editable()){event.preventDefault();s.add_rows(1);render();const all=[...element.querySelectorAll('[data-row][data-field]:not(:disabled):not([readonly]):not([hidden])')];all[controls.length]?.focus();}}
  });
  element.addEventListener('click',async event=>{
   const button=event.target.closest('[data-action],[data-error-row]');if(!button || button.disabled || s.busy)return;
   if(button.hasAttribute('data-error-row')){s.filters.errors=false;s.filters.nonzero=false;s.filters.search='';render();const row=[...element.querySelectorAll('[data-grid-row]')].find(row=>row.dataset.gridRow===button.dataset.errorRow);row?.scrollIntoView({block:'nearest'});row?.querySelector('[data-field]')?.focus();return;}
   const action=button.dataset.action;
   if(action==='add'){s.add_rows(1);render();element.querySelector('tbody tr:last-child [data-field]')?.focus();}
   else if(action==='prefill'){s.prefill();render();}
   else if(action==='delete'){s.delete_selected();render();}
   else if(action==='refresh'){if(await confirm_discard()){s.dirty=false;s.revision++;await s.load();}}
   else if(action==='save')await s.save();
   else if(action==='validate')await s.validate();
   else if(action==='activate'){const total=s.preview?.totals || totals(s.batch.rows,s.batch.currency,s.precision);const mode=s.batch.mode==='Takeover'?__('接管已有期初'):s.batch.mode==='Adjustment'?__('启用期初调整'):__('启用期初余额');if(await confirm_message(__('{0}\n公司：{1}\n切换日期：{2}\n本位币：{3}\n整批 {4} 行，借方 {5}，贷方 {6}，差额 {7}。\n本操作将影响正式余额，确认继续？',[mode,s.company,s.batch.opening_date,s.batch.currency,s.batch.rows.length,total.debit,total.credit,total.difference])))await s.activate();}
   else if(action==='adjust')await s.start_adjustment();
   else if(action==='template'){if(options.download_template)options.download_template();else root.open?.('/api/method/'+METHOD+'download_template','_blank','noopener');}
   else if(action==='import')open_import();
   else if(action==='apply-import'){s.apply_import();render();}
   else if(action==='cancel-import'){s.import_preview=null;render();}
  });
  function open_import() {
   if(options.import_file)return options.import_file(s);
   if(frappe?.ui?.FileUploader){new frappe.ui.FileUploader({allow_multiple:false,restrictions:{allowed_file_types:['.xlsx','.csv']},on_success:file=>s.preview_import({file_url:file.file_url})});return;}
   // Fixture previews use the same server-preview call without a second grid.
   const input=doc.createElement('input');input.type='file';input.accept='.csv';input.onchange=async()=>{const file=input.files?.[0];if(file)await s.preview_import({text:await file.text()});};input.click();
  }
  root.addEventListener?.('resize',layout);const observer=root.ResizeObserver?new root.ResizeObserver(layout):null;observer?.observe(element);
  const active=()=>options.active?options.active():true;
  const before_unload=event=>{if(active() && (s.dirty || s.busy)){event.preventDefault();event.returnValue='';}};root.addEventListener?.('beforeunload',before_unload);
  if(options.navigation_guard!==false && frappe?.set_route){const original=frappe.set_route;let bypass=false;
   frappe.set_route=function(...args){if(bypass || !active() || (!s.dirty && !s.busy))return original.apply(this,args);if(navigating)return Promise.resolve();navigating=true;return confirm_discard().then(ok=>{if(!ok)return;s.dirty=false;bypass=true;try{return original.apply(frappe,args);}finally{bypass=false;}}).finally(()=>navigating=false);};
   frappe.router?.on('change',()=>{if(active() || navigating || (!s.dirty && !s.busy))return;const requested=[...(frappe.get_route() || [])];navigating=true;bypass=true;original.apply(frappe,[['china-opening-balances']]);bypass=false;confirm_discard().then(ok=>{if(ok){s.dirty=false;bypass=true;original.apply(frappe,[requested]);bypass=false;}}).finally(()=>navigating=false);});
  }
  if(options.fixture){s.adopt(options.fixture);render();}else{render();if(options.company)s.load(options.company);}
  return editor;
 }
 return {Session,FocusMemory,create_link_control,mount,view,totals,to_minor,parse_tsv,payload,escape};
});
