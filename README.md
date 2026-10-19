# 中国财务本土化

`china_finance` 是面向 ERPNext v16 的中国财务合规应用。应用保留 ERPNext 原有总账过账机制，在其基础上提供法定会计凭证快照、对账单、税务发票、版本化财务报表、期间结账控制和电子档案。

## 协作入口

财务应用独立仓库为 [yueweiit/deeplinkerp-china_finance](https://github.com/yueweiit/deeplinkerp-china_finance)，从 `main` 分支更新后开发，PR 目标也选择 `main`。Frappe App 名保持 `china_finance`。
2026-10-16 的应用基线与线上 `b73f17138fdf8235e91a687b8a113cbb33e7031c` 完全一致，生产源码标签为 `production/2026-10-16/china_finance`。
整理前的主线保存在合并历史；上述生产标签及 `archive/2026-10-16/china_finance-before-production-alignment` 标签继续保留于原仓库 `yueweiit/DeepLinkERP`。跨应用对接见[独立仓库说明](https://github.com/yueweiit/deeplinkerp-deeplinkerp_branding/blob/main/docs/contributing/erp-branches.md)。
本次仅整理源码基线，没有执行部署、迁移或财务数据操作；后续发布仍按现有流程验收。

## 已实现模块

- 按公司配置企业会计准则或小企业会计准则；
- 独立的收、付、转、记凭证编号，不修改来源单据名称；
- 自动捕获所有已提交且产生总账分录的来源单据，生成不可变凭证快照；
- 客户、供应商和银行对账快照；
- 招商银行、中国银行、农业银行、工商银行和广发银行文字版回单解析、历史凭证补回单、确认生成草稿、跨来源去重及按公司配置社保、公积金分摊；
- 进项、销项数电票，支持蓝票、红票、作废票及多对多业务单据分摊；
- 电子原始凭证 SHA-256 校验和私有结账档案包；
- 版本化财务报表模板、科目映射、比较期间和结账报表快照；
- 中国凭证账簿、凭证完整性检查、增值税台账、增值税申报底稿和业务单据链报表；
- 现金流量指定单列表内直接指定、拆分、保存草稿和整单确认，保留人工指定及原生权限校验；
- 提供独立的中国财务工作区、桌面入口和侧边栏，集中展示当前可用的中国财务功能；
- 基于 ERPNext `Period Closing Voucher` 和公司会计冻结日期的结账检查。

应用不使用 Server Script，不执行生产环境手工 SQL，不修改 ERPNext 核心代码，也不覆盖地区总账逻辑。

## 进入中国财务

点击桌面“中国财务”或侧边栏“中国财务工作台”，先选择公司，再进入工作台。工作台顶部显示当前公司，可点击切换。从工作台或中国财务侧边栏打开的报表、带公司字段的列表、科目映射控制台和银行对账，自动带入所选公司；从已筛选的列表新建单据时沿用该公司。

“查凭证”菜单保留，进入时直接带入公司，不再单独弹出选择框。选择仅用于当前用户当前浏览器标签页的中国财务导航，不修改系统默认公司或已有单据。详见[中国财务入口选择公司](docs/中国财务入口选择公司.md)。

## 兼容环境

- Frappe / ERPNext `v16.23.0`
- Python `3.14`
- MariaDB `11.8`
- Node.js `24`

## 公司启用

应用随自定义镜像发布后，按公司执行初始化。启用日期是法定凭证切换日；切换日前的总账分录仍可在 ERPNext 报表中查询，但不会倒排法定凭证号。

新建中国财务设置及公司自动初始化默认采用“小企业会计准则”；需要“企业会计准则”时可显式选择。更新应用或重复初始化不会覆盖已有公司的会计准则。

```bash
bench --site SITE execute china_finance.api.initialize_company --kwargs \
  '{"company":"公司全称","accounting_standard":"小企业会计准则","taxpayer_type":"一般纳税人","activation_date":"2026-01-01","enforce_role_separation":1}'
```

初始化会为资产负债表和利润表生成建议科目映射，所有建议映射默认处于“未复核”状态，必须由财务人员确认。现金流量表和所有者权益变动表取决于企业会计政策及现金流分类，需手工配置。

## 公共接口

银行回单入口、历史与新业务处理、悦为社保分摊规则及服务器更新步骤见[银行回单导入](docs/银行回单导入.md)。

草稿查改、未记账试算和按月统一记账见[凭证准备与月末记账操作](docs/凭证准备与月末记账操作.md)。

现金流量项目的行内指定、拆分与批量确认见[现金流量指定单列表处理](docs/现金流量指定单列表处理.md)。

已用标准模板新建、尚未录入业务的公司，可参考[新公司科目模板与启用](docs/新公司科目模板与启用.md)，预览并同步至应用内置的悦为公司科目模板，然后配置小企业会计准则及正式记账日期。

- `china_finance.api.initialize_company`：初始化公司设置和建议科目映射；
- `china_finance.api.deployment_health`：检查部署结构和标准数据；
- `china_finance.services.voucher.rebuild_missing_vouchers`：幂等补建切换日后的缺失凭证；
- `china_finance.services.reconciliation.generate_statement`：生成客户、供应商或银行对账快照；
- `china_finance.services.tax_invoice.import_invoices`：幂等导入税务发票；
- `china_finance.services.closing.preview_closing_checks`：预览结账检查；
- `china_finance.services.closing.reopen_closing`：按审计要求重新开账；
- `china_finance.services.archive.verify_archive`：复核档案文件 SHA-256；
- `china_finance.services.archive.export_archive_package`：导出私有电子档案包。

所有批量接口均返回处理数、成功数、跳过数、失败数和逐条错误，不会静默跳过失败记录。

## 旧财务数据核查

管理员可通过 `bench execute china_finance.services.finance_cleanup.preview_finance_cleanup`，按公司及日期只读预览旧凭证、关联记录和清理阻塞项。另提供 `china_finance.services.cancelled_finance_cleanup.cleanup_cancelled_finance`，根据已保存的预览，清理已取消的测试凭证及符合条件的关联记录；无来源会计快照和取消后的支付分类账残留分别通过显式开关纳入。默认只预览，执行删除须传入 `apply:1` 并开启维护模式。[旧财务凭证清理操作手册](docs/旧财务凭证清理.md) 包含服务器单行命令、站点私有目录中的预览文件、备份与复核步骤、常见报错处理，以及 2026-09-20 的实际清理记录。

## 发布约束

应用必须能够进入现有自定义镜像，并兼容服务器既有的 `deploy_akivision_apps.sh` 更新流程。应用代码不得要求在服务器上执行额外手工 SQL、临时补丁或应用外数据修复。

### 预生成报表通知兼容补丁

`china_finance` 通过标准 `after_build` 钩子交付一处 Frappe `v16.23.0` 兼容修复：预生成报表的 `report_generated` 通知在事务提交后发送，避免浏览器收到完成通知时尚不能读取结果附件。代码入口为 `china_finance/build.py`，补丁随 App 保存在 `china_finance/framework_patches/frappe/prepared-report-after-commit.patch`，不改变财务数据、报表计算或权限。

这是一处明确维护的 Frappe 源码补丁，不是运行时全局替换。`bench init --apps_path` 获取包含本 App 的应用清单后，会在正常构建中调用该钩子；已有 Bench 的 `bench build --app china_finance` 同样会调用。无需新增根 Docker 仓库的构建步骤，也无需从本地 Frappe 分支拉取代码。必须先将本 App 提交推送到组织仓库，服务器正常构建取得包含该提交的 `main` 并发布新镜像后才会生效；本地提交、仅迁移站点、跳过构建或重启旧镜像均不算完成交付。

补丁已存在时保持源码不变；上游源码不兼容、缺少补丁或无法写入时构建失败，应核对上游修复后更新或移除补丁，不能忽略错误。补丁使用构建环境现有的 Git，不要求保留镜像内的 `.git`。升级 Frappe 时须复核；同一 Bench 共享的 Frappe 预生成报表均受此通知时序修复影响。

构建日志和 Bench 根目录的 `china-finance-framework-patches.json` 记录补丁名、SHA-256、交付 App 与应用状态；原 App 源提交记录不包含对 Frappe 的这处修改，验收时应同时核对该清单。例如在主机只读检查新镜像：

```bash
docker run --rm --entrypoint cat <本次镜像标签> /home/frappe/frappe-bench/china-finance-framework-patches.json
```

隔离回归从 Bench 的 `sites` 目录执行，不连接数据库、不删除预生成结果：

```bash
../env/bin/python -m unittest china_finance.tests.test_framework_patches china_finance.tests.test_prepared_report_notification -v
```

本地已通过 8 项回归，并使用 Frappe 真实 `run_after_build_hook` 对官方 `v16.23.0` 的完整目标文件验证应用及重复执行；App wheel 构建成功并核对钩子与补丁文件均已打包，CI YAML 和 Bash 步骤语法检查通过。没有运行完整镜像构建或生产发布。实时页面刷新还依赖正常的 Socket.IO 服务，该补丁不调整网络配置。

## 许可证

MIT
