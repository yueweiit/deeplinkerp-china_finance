frappe.pages['china-opening-balances'].on_page_load = function(wrapper) {
 const page=frappe.ui.make_app_page({parent:wrapper,title:__('期初余额'),single_column:true});
 frappe.breadcrumbs.add('China Finance');
 const container=document.createElement('section');page.main.get(0).appendChild(container);
 wrapper.opening_balance_ready=frappe.require(['/assets/china_finance/js/china_opening_balance.js','/assets/china_finance/css/china_opening_balance.css']).then(async()=>{
  await frappe.model.with_doctype('Journal Entry Account');
  const company=frappe.route_options?.company || window.china_finance?.company_context?.default_company() || '';
  wrapper.opening_balance_context_company=company;
  wrapper.opening_balance_editor=window.china_finance.opening_balance.mount(container,{frappe,company,active:()=>frappe.get_route()?.[0]==='china-opening-balances'});
  return wrapper.opening_balance_editor;
 });
};
frappe.pages['china-opening-balances'].on_page_show = async function(wrapper) {
 frappe.utils.set_title(__('期初余额'));
 const editor=await wrapper.opening_balance_ready;
 const company=frappe.route_options?.company || window.china_finance?.company_context?.get_company();
 if(company && company!==wrapper.opening_balance_context_company){
  if(company!==editor.session.company)await editor.session.change_company(company,editor.confirm_discard);
  wrapper.opening_balance_context_company=company;
 }
};
