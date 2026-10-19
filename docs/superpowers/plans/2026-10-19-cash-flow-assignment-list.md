# 现金流量指定单列表处理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 在现有现金流量指定单列表内完成指定、拆分、保存、整单确认，测试通过后发布服务器。

**Architecture:** 保留现有 DocType、表单和 `cash_flow_assignment` 的原生保存/提交校验，只为现有列表增加处理视图和受权限控制的批量服务。列表按指定单分页、按现金总账分录组织拆分行；保存与确认分开，每单使用锁、版本检查及保存点，逐单返回结果。原表单继续承担作废和修订，无新的业务状态或平行业务流程。

**Tech Stack:** Frappe/ERPNext v16.23.0、Python 3.14、现有 JavaScript、Frappe ListView、原生事务测试和 Node tests。

## 基线与已核查复用

- 用户于 2026-10-19 确认交互原型并授权测试后部署。
- 首次 fetch 的原仓库 `china_finance@16f4515` 已过时；线上当天已迁移独立应用仓库。
- 实施基线为 `yueweiit/deeplinkerp-china_finance/main@80f2a704bdccff37c67307994bfe1b64e6144b13`，与当前运行 Finance 一致。开发分支 `codex/cash-flow-list`。
- 保留新基线月结推迟现金流量确认的既有设置与检查，不把旧包覆盖到新镜像。
- 复用 `get_cash_flow_row_options`、`_refresh_draft_cash_legs`、`prepare_cash_flow_assignment_confirmation`、DocType `validate/before_submit`、公司上下文及权限模型。
- 不使用 `reload_cash_flow_assignment_suggestions` 实现批量采纳：它整表替换。列表仅本地填入空白且未拆分的行，再走相同保存入口。原表单仍调用旧接口，不能删除；待旧表单重新载入建议行为另行迁移并验证无调用后才能移除。

## Task 1: 原生列表服务与测试

Files: `china_finance/services/cash_flow_assignment.py`（必要时提取紧邻的列表模块）、`china_finance/tests/test_cash_flow_assignment_list.py`。

- [x] 先运行新增测试，记录缺少列表接口的 RED。
- [x] `get_cash_flow_assignment_list(company, from_date, to_date, status, search, direction, start, page_length)` 返回完整单据/子行、逐单项目选项、读写/确认权限、阻塞原因、总数及状态计数。查询使用文档和公司权限；收付方向过滤命中单据但返回该单全部现金行。
- [x] `process_cash_flow_assignments(assignments, action)` 接收 `name/modified/items/remarks`；项目/金额/备注是允许修改的字段。子行 name 和 GL 必须属于本单；新增拆分从本单权威现金行复制证据字段。
- [x] 每单加锁、比较 `modified`，使用保存点；调用原生 `save/submit`，职责分离保持有效，失败回滚本单并保留其他逐单结果。禁止 `ignore_permissions`、手工 commit 或直接写状态。
- [x] 运行真实数据库测试，覆盖拆分、整单多行、分页/搜索/方向、公司与角色权限、版本冲突、伪造字段、失败回滚和人工值保留。

## Task 2: 现有列表处理界面

Files: `china_finance/public/js/china_cash_flow_assignment_list.js`、列表 CSS、`china_finance/hooks.py`、`china_finance/tests/test_cash_flow_assignment_list.js`。

- [x] 对编辑模型、去重现金发生额、按整数分校验拆分、空白建议保留人工值、版本/异步响应和整单选择先运行 RED。
- [x] 仅挂载本 DocType 的 ListView 实例，保持现有路由与 Frappe shell；统一刷新入口，避免原生增量刷新重写编辑区。
- [x] 依已确认原型完成列表筛选、状态页签、行内项目、金额拆分、批量建议/统一指定、草稿保存、整单确认和侧边详情。
- [x] 保存可容纳未配平但字段完整的草稿；确认必须整单配平。按服务端 `can_write/can_confirm` 控制；已确认及作废只读。原生表单仍可打开。
- [x] 防止筛选、分页、实时刷新和导航静默丢失未保存内容；请求进行时禁止重复处理，陈旧响应不得覆盖新编辑。

## Task 3: 集成验收和评审

- [x] 新建独立 MariaDB/Redis/站点，挂载最新 Finance 工作区；不改旧 QA 或线上数据。
- [ ] 跑现有相关 Node 测试及原生应用测试，执行重复迁移、资产构建和部署健康检查。
- [ ] 在真实 Frappe 页面验证行内编辑、保存、拆分、批量部分失败、整单确认、只读和公司/权限；测试仅使用合成凭证。
- [x] 先需求符合性、再代码质量评审；修复明确问题并复核。

## Task 4: 冻结发布与线上验收

- [ ] 更新最新 main、冻结提交、推送 PR 并完成相关 CI；合并到独立仓库 main。
- [ ] 发布前再次冻结线上镜像和所有应用 SHA，沿用现有锁、备份、迁移/资产和健康流程。若其他应用正在发布，等待稳定后重新核对。仅引入本次 Finance 变化，逐应用核验其他源码未退回。
- [ ] 登录线上核验现有列表路由、请求权限、数据展示、详情和已确认只读；不为验收改写真实财务单。
- [ ] 交付上线提交/镜像、测试证据、实际页面结果、新增/修改/删除、代码与测试数量变化、相关技术债。临时原型不是正式测试证据。

## 已取得证据

- 现有前端基线 40 项通过；本轮前端现共 59 项通过，新增 19 项业务/交互场景。
- 列表服务原生模块 23 项通过，包含真实 Journal Entry 提交生成 GL、凭证快照及指定单，再拆分保存确认而不重记来源的集成场景。
- 新增摘要读取和检索遵守中国会计凭证自身权限，拒绝借指定单权限泄露摘要；只读查询零持久化。
- 独立需求、代码质量评审通过；修复原有子行对象比较误改指定人，并验证项目修改、拆分及等额 GL 腿交换均不能绕过职责分离。完整回归与真实页面验收继续进行。
- 原 CI 基线失败于缺少 pytest；完整本地回归又发现既有中文断言在默认英文环境下失败。CI 已显式安装测试依赖并指定 `lang=zh`，未改业务规则或删减测试。
- 独立 QA 资产构建通过；站点使用独立 MariaDB 11.8.6、Redis、sites volume，不改旧 QA 记录。
- 旧取消同步测试显式选择其断言使用的中文；旧费用周转测试保持原英文上下文并按翻译定位导出列，保留所有数值校验。

## 后续技术债

- 列表按完整单据分页，但筛选和计数仍需读取符合条件的父单身份；大数据量性能尚未压测。
- 原费用周转报表以英文 Total 字符串识别合计行；中文上下文的历史合计处理需要独立修复。本轮未改无关报表，也未降低原数值断言。
