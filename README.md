# 闭环管理废弃物回运责任基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

## 废弃物回运责任项目

针对年度撤站中“一箱混装、总重量吻合但无人能证明每类废弃物的许可、暂存和接收责任”的问题，服务在基础能力上扩展了完整的回运责任链：

- 产生地登记分类（废油、废电池、受污染包装）与数量；
- 封箱时校验混装限制与重量上限，并把适用法规版本（混装限制、各类期限、责任人资格要求）整体哈希快照**冻结**在箱子上，新版法规不能改写旧箱事实；
- 暂存、跨营地移交、承运、目的地接收均为“提出 → 接收方责任人确认”的双方流程，确认时责任与期限义务同时转移；
- 容器破损、数量修正、运输取消、接收拒绝一律通过**新的冲销/取消/重装事件**处理，已确认责任不可回滚；破损箱必须拆箱重装才能继续流转；
- 全部业务事实写入只增不改的 `waste_events` 台账（并镜像进哈希串联审计链），支持从项目来源追到处置凭证，也支持从一个箱子反查全部组成与重装谱系、从一个批次反查经过的每个箱子；
- 重复回调与进程恢复依靠 `request_id` 幂等回执和 `callback_token` 核销，不重复转移责任；
- 协调员视图给出当前责任方、逾期节点、批次/箱子/移交清单三层数量差异（含已被冲销事件解释与未解释短少的区分），而不是只对总重量。

责任链状态：`sealed → staged → in_transit → delivered → disposed`，异常分支为 `damaged` 与 `repacked`；全部批次处置完毕且无未结义务时，处置凭证关闭整个项目。

### 废弃物接口

写入均为 POST，需 `X-Actor-Id` 且支持 `request_id` 幂等：

`/waste/regulations`、`/waste/custodians`、`/waste/shipments`、`/waste/items`、
`/waste/containers/seal`、`/waste/containers/repack`、
`/waste/handovers`（propose）、`/waste/handovers/confirm`、`/waste/handovers/reject`、
`/waste/handovers/cancel`、`/waste/damage`、`/waste/corrections`、`/waste/disposals`。

查询（GET）：

- `/waste/shipments/{id}/events`：项目正向事件链；
- `/waste/shipments/{id}/dashboard`：当前责任方、未结/逾期义务、数量差异；
- `/waste/containers/{id}`：箱子当前组成、封箱冻结快照与重装谱系；
- `/waste/items/{id}/events`：批次反查经过的全部箱子与移交；
- `/waste/obligations`、`/waste/overdue`：未结与逾期责任节点。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  其中 `waste_*.py` 构成废弃物回运责任项目；
- tests/：基础规则、事务边界、接口路由、废弃物责任链和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance
    PYTHONPATH=src python3 -m polar_station_foundation.waste_acceptance

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，核对幂等回执与审计链。废弃物验收额外演完混装拦截、法规冻结、双方确认移交、破损冲销重装、运输取消、接收拒绝、逾期义务与处置凭证关闭，并核对双向溯源和数量差异；成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
