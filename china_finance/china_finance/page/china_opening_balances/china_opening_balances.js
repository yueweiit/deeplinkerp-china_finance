frappe.pages['china-opening-balances'].on_page_load = function(wrapper) {
 const page=frappe.ui.make_app_page({parent:wrapper,title:__('期初余额'),single_column:true});
 frappe.breadcrumbs.add('China Finance');
 const container=document.createElement('section');page.main.get(0).appendChild(container);
 wrapper.opening_balance_ready=frappe.require(['/assets/china_finance/js/china_opening_balance.js','/assets/china_finance/css/china_opening_balance.css']).then(async()=>{
  await frappe.model.with_doctype('Journal Entry Account');
  const company=frappe.route_options?.company || window.china_finance?.company_context?.default_company() || '';
  const context=window.china_finance?.company_context;
  wrapper.opening_balance_context_company=company;
  wrapper.opening_balance_editor=window.china_finance.opening_balance.mount(container,{frappe,company,active:()=>frappe.get_route()?.[0]==='china-opening-balances',on_company:async value=>{
   wrapper.opening_balance_context_company=value;
   try{await context?.change_company(value,{apply:false,guard:false});}catch{frappe.msgprint(__('无法确认公司，请检查权限或网络后重试'));}
  }});
  const editor=wrapper.opening_balance_editor;
  context?.register_page('china-opening-balances',{
   before_switch:()=>{if(editor.session.loading || editor.session.busy){frappe.msgprint(__('当前页面尚未准备好，请稍后再切换公司'));return false;}return editor.confirm_discard();},
   capture:()=>({restore:context.capture_session(editor.session),company:wrapper.opening_balance_context_company}),
   restore:snapshot=>{snapshot.restore();wrapper.opening_balance_context_company=snapshot.company;},
   switch_company:async value=>{
    const loaded=editor.session.company===value || await editor.session.change_company(value,()=>true);
    wrapper.opening_balance_context_company=editor.session.company;
    return !!loaded && editor.session.company===value;
   }
  });
  return wrapper.opening_balance_editor;
 });
};
frappe.pages['china-opening-balances'].on_page_show = async function(wrapper) {
 frappe.utils.set_title(__('期初余额'));
 const editor=await wrapper.opening_balance_ready;
 const context=window.china_finance?.company_context;
 const company=frappe.route_options?.company || context?.get_company();
 if(company && company!==wrapper.opening_balance_context_company){
  if(company!==editor.session.company)await editor.session.change_company(company,editor.confirm_discard);
  wrapper.opening_balance_context_company=editor.session.company;
 }
 if(editor.session.company && editor.session.company!==context?.get_company()){
  try{await context?.change_company(editor.session.company,{apply:false,guard:false});}catch{frappe.msgprint(__('无法确认公司，请检查权限或网络后重试'));}
 }
};
