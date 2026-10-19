# 现金流量指定单紧凑列表实施计划

> **For agentic workers:** Use subagent-driven-development for the independent frontend and backend tasks, then perform specification and quality reviews before release. The user explicitly authorized implementation and deployment; no additional design approval is required.

**Goal:** 将现有现金流量指定列表替换为采购订单风格的连续表格，在列表完成项目选择、保存和整单确认，并验证并发、取消、权限及真实报表结果。

**Architecture:** 保留原生 List 路由、Session、整单 payload、角色/公司权限、原生 save/submit 及报表消费者。只替换既有卡片渲染，不新增另一套业务入口；加固现有事务与来源门禁。采购订单最终 CSS tokens 用于视觉一致性，横向滚动保留独立的细滑块和固定身份列。

**Tech Stack:** Frappe/ERPNext v16、Python/MariaDB、原生 JavaScript/jQuery/CSS、Node test、隔离 Frappe 原生 QA。

## 基线、授权与复用

- GitHub `yueweiit/deeplinkerp-china_finance` 已 fetch；HEAD 和 origin/main 均为 `03e9f05a4d1a53e569df300538224d90eda9b664`，已有独立 checkout 干净。
- 工作分支 `codex/cash-flow-grid-refinement`，继续使用 `/Users/smk/.codex/worktrees/cash-flow-list-current/deeplinkerp-china_finance`，不改其它应用 checkout。
- 用户要求直接部署，审美/配色沿用采购订单，列表内选择/切换应真正生效；保存草稿与整单确认仍明确区分，不能自动替财务确认或绕过职责分离。
- Session 保留整数分、脏数据保护、过期响应丢弃、权限、批次 busy、失败保留编辑、导航拦截。`data-action/name/index/edit/select` 继续调用同一服务。
- 保存调用 `china_finance.services.cash_flow_assignment.process_cash_flow_assignments`，全量整单 payload：`{name,modified,remarks,items:[{name,gl_entry,cash_flow_row_code,assigned_amount,remarks}]}`；action 为 save/confirm。确认生效继续由原生 submit 及 `get_confirmed_cash_flow_values` 进入原报表。
- 删除被连续表格替换的卡片模板/CSS和确已无调用的卡片辅助逻辑；保留原单表单、详情抽屉、历史单只读，它们承担独立业务约束，不是重复实现。

## Task 1：锁、证据和权限加固

**Files:** `china_finance/services/cash_flow_assignment_list.py`、`china_finance/services/cash_flow_assignment.py`、`china_finance/china_finance/doctype/china_cash_flow_assignment/china_cash_flow_assignment.py`；优先扩展原 `test_cash_flow_assignment_list.py`，必要时复用原生取消并发测试 fixture。

- [ ] 用现有测试增加真实风险断言并观察失败：反向重叠批次、重复名称、死锁/超时导致整个事务失效、旧快照下来源取消、原生 API 篡改现金证据、跨公司重建。
- [ ] 批次先验证身份、拒绝重复，以固定顺序获取全部来源门禁再获取指定单锁，结果按原输入顺序返回；数据库 deadlock/timeout 必须抛给事务拥有者，不返回已整体回滚的逐单成功，也不回滚已消失的 savepoint。
- [ ] 所有用户写入口与原生 save/submit/cancel 共享早于指定单锁的来源门禁；保持既有取消 worker 的 source → issue → assignment → voucher 顺序，不能在 before_submit 才追加反序来源锁。写时来源、凭证及 GL 使用当前读；列表只读诊断不加写锁。
- [ ] 原生草稿现金证据必须与当前合法 GL leg 一致；保留合法加工后的项目/金额/说明和拆分，不允许 read_only 字段伪造收付方向。保留明确且唯一的临时 GL 修复能力。
- [ ] 重建先检查原指定单、Company/create 权限；自动来源创建内部的权限豁免不向公开重建入口泄漏。
- [ ] 原生 QA 中执行两连接反向批次及取消竞争，所有线程有超时、有终态，不只靠模拟调用顺序证明无死锁。
- [ ] 原生保存/确认后的报表金额、覆盖率和自动回退去重正确；源凭证、总账和审计凭证不因指定而重记账。

## Task 2：连续表格与采购风格

**Files:** `china_finance/public/js/china_cash_flow_assignment_list.js`、`china_finance/public/css/china_cash_flow_assignment_list.css`、`china_finance/tests/test_cash_flow_assignment_list.js`；CSS cache 版本在 `china_finance/hooks.py` 更新。

- [ ] 先扩展原 JS/DOM 测试，覆盖整单 rowspan 与拆分原始 index、输入期间不重挂载、blur 后首击操作、滚动位置保持、控制状态恢复、选择/保存/确认真实请求及失败保留。
- [ ] 两行紧凑操作/筛选；一张共享表头连续表；每个指定单一个 tbody，整单 checkbox/凭证身份各一次，普通明细最小 34px，完整现金/银行科目可换行；摘要/对方科目/说明及审核记录在展开区或详情中访问。
- [ ] 复用采购最终配色：border #e1e8f0、header #f4f7fb、alternate #f8fafc、hover #edf5ff；控件 30px/13px/4px 圆角。不要新增自造大卡片或粗重滚动控件。
- [ ] 固定 checkbox 和凭证列，表头交叉区背景不透明；正文单一 viewport。底部横条 14px 命中区、6px 浅灰滑块、4px 轨道，无深边框，鼠标与键盘左右/Home/End均有效，双向同步；渲染和展开后保持/重算位置。
- [ ] 保留列表内项目 Select、指定金额、拆分、说明、填建议、统一指定、保存更改、整单确认和逐单结果；未保存与已保存/已确认状态清楚，选择不会假装已提交。
- [ ] 按采购页面的空间逻辑约束本页面，不改全站导航、其它页面宽度和公用权限。已有 form 入口仍提供详情/只读查询。

## Task 3：串行原生验收、审查与发布

- [ ] Root 独占隔离原生 QA 测试；其它代理不同时改同一测试 DB。前后端修改文件分别归属代理，统一由 root 提交，避免 git index 竞争。
- [ ] 基线/针对性测试：`node --test china_finance/tests/test_cash_flow_assignment_list.js`；`docker exec -w /home/frappe/frappe-bench dlp-cash-flow-list-qa /usr/local/bin/bench --site cash-flow-list-qa.localhost run-tests --app china_finance --module china_finance.tests.test_cash_flow_assignment_list`。沿用本任务既有 QA site 的原生测试入口。
- [ ] 浏览器在真实 QA 页面验收选择 → 保存 → 切换/刷新保留；确认 → 原报表金额生效；已确认只读；金额/方向/公司/版本冲突准确；未保存切换可取消且编辑不丢；鼠标左右拖、固定列、1366/1492/1920及窄屏布局。
- [ ] 规格审查通过后做独立代码质量审查，修完所有 P1/P2；运行受影响测试和相关 CI，冻结最终 main SHA 与实际候选，不用旧截图冒充最终构建。
- [ ] 发布沿用原 ERP/run/bench 锁、完整备份、精确 app-revisions/源码多应用守卫。当前基镜像 b27 的 Finance03e9、其它12应用版本和源码须保留；静态资源原路径采用 cache 版本并清缓存，不重编其它应用。
- [ ] 发布进程退出后验证六服务、源码、健康、登录页面和实际资源，再使用既有 source retarget 更新新 image pin，保留 Branding2b92/runner checksum/logging receipt。迁移前后核对原生任务配置，不开启全局 scheduler。
- [ ] 当前源同步后续轮次有新失败，单独只读定位；不能把21:00历史成功当当前健康，也不为恢复成功放宽来源/审批/金额/身份规则。
- [ ] 正式业务页面仅做只读验收，不为测试修改真实财务指定；真实写入与报表效应在隔离环境完成。部署后观察自然来源轮次，分别报告源同步与下游业务结果。
- [ ] 交付新增/修改/删除文件、行数、独立测试与参数场景变化、删除的替代实现、CI/部署/真实浏览器证据以及未处理技术债。
