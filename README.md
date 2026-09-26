# 专利与技术秘密档案管理服务

这是一个面向研发机构、法务部门和保密办公室的模块化后端，集中管理专利交底资料、技术秘密载体、移交批次、受控副本签发、查阅借阅、对外披露、归还、合规处置、载体盘点、版本与载体来源、密级库位、泄密事件、登录权限、审计以及可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 已有能力

- 身份与权限：支持引导管理员、登录、会话、用户、角色和细粒度权限。
- 批次与二维码：移交批次保存项目、数量和稳定二维码载荷。
- 档案登记：登记专利交底、工艺文档、源代码介质等资产，保存密级库位和生命周期状态。
- 受控副本签发：一次事务内扣减来源载体、创建副本、记录损耗和版本来源事件。
- 查阅借阅归还：保存查阅用途、到期时间、部分归还和最终归还状态。
- 对外披露登记：使用幂等键登记合作方、披露范围和载体消耗，防止重复请求二次扣减。
- 位置脱敏：普通权限只能看到受限库位的替代码，授权人员可查看精确位置。
- 双人审批：合规处置、敏感库位解密等高风险操作要求申请人与审批人分离，并累计不同审批人的决定。
- 泄密事件追踪：事件可以关联档案或移交批次，保存严重度、调查状态和处置结果。
- 权属协作：登记多名发明人的贡献份额、权属单位与有效期；提交转让/补充/撤回协议时校验份额总和与签署人资格，跨单位转让进入双人复核；每次生效生成不可变的版本化权属快照，生效前后均可按时间查询到当时正确的权属。
- 权属事件链与下游引用：协议重复提交幂等不产生二次转让，撤回与补充协议形成哈希链式事件；交底版本、专利家族和对外披露自动引用当时有效的权属快照。
- 审计与任务：关键身份及业务操作留痕，后台任务支持去重、领取与完成。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/archives.db`，可用 `ARCHIVE_DATABASE_PATH` 指定其他路径。

## 初始化与完整性检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```bash
python -m pytest
```

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

## 权属协作流程

1. 法务先维护权属单位：`POST /api/ownership/units`。
2. 对交底档案提交初始权属协议 `POST /api/ownership/dossier/{id}/agreements`，请求体携带每名发明人的 `share`（总和必须为 1）、`unit_code`、`effective_at/expires_at`，以及与发明人账户对应的 `signer_user_ids`。
3. 同单位变更提交后立即生效并生成新版本快照；检测到跨单位转让时进入 `pending_review`，需两名与申请人不同的复核人通过 `POST /api/ownership/agreements/{id}/reviews` 完成双人复核。
4. 查询权属：`GET /api/ownership/dossier/{id}/current`（可用 `?at=` 做时间旅行）；协议与链式事件见 `/agreements`、`/events`。
5. 协议使用 `idempotency_key` 幂等，重复提交不会二次转让；补充（`supplement`）与撤回（`withdrawal`）须通过 `supersedes_agreement_id` 指向原协议并留下链式事件。
6. 受控副本（交底版本）、对外披露在创建时自动钉住当时有效快照，专利家族 `POST /api/ownership/families` 同样引用根交底书的有效权属；可用 `GET /api/ownership/references/{ref_kind}/{ref_id}` 反查。
