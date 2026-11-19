# 网关测量帧解码台

工厂数据员上传网关的两寄存器测量帧（CSV），平台按固件登记的字序把 8 位十六进制
原始字解成 IEEE 754 单精度读数。阀门开度稳定时多台设备会反复上报相同测量字，因此
**载荷（固件号 + 原始字）只存一份、共用解码结果**；每台设备的每一帧仍逐帧保留
原文、状态、读数与各自的时间。

## 数据分层

| 层 | 表 | 内容 |
| --- | --- | --- |
| 载荷层 | `payloads` | 去重后的（固件号, 原始字），`UNIQUE(fw_version, raw_word)`；同固件同原始字只有一行，解码结果（状态、读数、位型、字序、解码时间）存这一层，被所有引用帧共用 |
| 帧层 | `frames` | 每收到一帧一行，引用其载荷，保留设备号、帧号、`received_at` 与该帧自己的 `decoded_at` |
| 固件层 | `firmware` | 不可变的固件字序登记 |

## 业务规则

| 规则 | 实现 |
| --- | --- |
| CSV 列：设备号、帧号、固件号、8 位十六进制原始字 | `app/decoder.py:parse_csv`；支持中文/英文表头，字段可用引号包裹 |
| 每批至多 200 帧 | 解析阶段整批拒绝，不写入 |
| 同固件号同原始字的载荷只存一份 | `payloads` 唯一约束；多设备/多帧引用同一载荷行，共用一份解码结果 |
| 首页显示实际帧数与载荷份数 | `service.totals`；设备表与固件表也分别列出帧/载荷份数 |
| 设备页与查询 API 仍逐帧返回原始字、状态、读数和时间 | `service.device_frames` 经 JOIN 还原每帧字段，帧身份仍为设备号 + 帧号 |
| 同设备同帧号同内容重传沿用结果 | 不新增行、不重算，帧的 `received_at`/`decoded_at` 保持原值 |
| 同设备同帧号异内容整批拒绝 | 写入前全量比对，事务回滚，页面列出冲突行 |
| 已登记固件按高字在前 / 低字在前解码 | `high_first` / `low_first`，来自 `firmware` 表的不可变记录 |
| 非有限值（NaN、±Inf）标无效 | 读数为空，状态 `invalid`，保留原始位型 |
| 未知固件载荷标待解，不猜读数 | 载荷状态 `pending`，`reading`、`bit_pattern` 与帧 `decoded_at` 均为空 |
| 登记字序后补解只写载荷层、每份待解载荷只解一次 | 同一 `BEGIN IMMEDIATE` 事务内 `UPDATE payloads ... WHERE status='pending'`；再把引用这些载荷的待解帧 `decoded_at` 统一记为本次补解时间 |
| 新帧复用已解载荷时解码时间按本次接收计 | 帧 `decoded_at = received_at = 本次时间`，载荷层结果与载荷 `decoded_at` 不变 |
| 上传与登记交错时只见同一已提交状态 | 上传、补解、查询都在写事务/读快照中串行化，页面读同一快照，不会看到"已配置却仍待解" |
| 不能重复或错序解码 | 补解带 `status='pending'` 守卫；字序一经登记不可更改 |
| 失败保留原状态、中断可重试 | 任何异常都 `ROLLBACK`，整批不落地；迁移也在单事务内，失败后旧库完整、重启重试 |
| 已有数据卷启动时自动升级 | `PRAGMA user_version` 管理；旧版宽表 `frames` 在一个事务内无损迁移为载荷层 + 帧层，保留每帧原字段和值（见下） |
| 重启后可查每帧原文、状态、有效读数 | SQLite 持久化于 `/data`，`/devices/<设备号>` 与 JSON API 可查 |
| docker compose 启动 | `Dockerfile` + `docker-compose.yml`，数据卷持久化 |

"内容"指原始字与固件号；设备号 + 帧号是帧的不可变身份。

### 解码时间语义

| 场景 | 载荷层 `decoded_at` | 帧层 `decoded_at` |
| --- | --- | --- |
| 未知固件先到 | 空 | 空（待解） |
| 登记字序补解 | 本次补解时间，每份载荷一次 | 所有引用帧统一记本次补解时间 |
| 新帧引用已解载荷 | 不变 | 本次接收时间（= 该帧 `received_at`） |
| 同号同内容重传 | 不变 | 沿用原帧时间，不改动 |

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在 compose 卷 `frame-data` 中（容器内 `/data`），重建容器不丢数据。

本地不带容器运行：

```bash
pip install -r requirements.txt
DATA_DIR=./data python -m gunicorn --bind 127.0.0.1:8000 wsgi:app
```

## 页面

- `/` 上传 CSV、登记固件字序、查看实际帧数/载荷份数、设备与固件列表
- `/devices/<设备号>` 该设备每帧的帧号、固件号、原始字、状态、读数、字序、解码位型、收到时间、解码时间
- `/healthz` 健康检查（容器 HEALTHCHECK 使用）

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/upload` | 请求体为 CSV 文本（`text/csv`）或 `multipart/form-data` 的 `csv` 文件；成功 200，异内容整批拒绝 409。`summary` 含 `new_payloads`（本批新增载荷份数） |
| POST | `/api/firmware` | JSON `{"fw_version": "...", "word_order": "high_first"\|"low_first"}`；新建 201，重复登记同字序 200，改字序 409。返回 `backfilled`（补解载荷份数）与 `frames_backfilled`（关联帧数） |
| GET | `/api/devices` | 设备列表与各设备帧数、载荷份数、待解数 |
| GET | `/api/devices/<设备号>` | 每帧原文、状态、有效读数、解码位型、收到/解码时间 |
| GET | `/api/firmwares` | 已登记固件及其字序、载荷份数、帧数、待解载荷数 |

示例：

```bash
curl -X POST --data-binary @sample_frames.csv -H 'Content-Type: text/csv' \
     http://localhost:8000/api/upload
curl -X POST -H 'Content-Type: application/json' \
     -d '{"fw_version":"FW-2.3.1","word_order":"low_first"}' \
     http://localhost:8000/api/firmware
```

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：两种字序的编码/解码、NaN/±Inf、批发上限、批内自相矛盾、重传沿用、
异内容整批拒绝、登记先后顺序、字序不可更改、**同载荷跨设备只存一份且只解一次**、
补解只写载荷层并回填帧时间、**新帧复用载荷按本次接收计解码时间**、
并发上传与登记竞争、**旧数据卷自动迁移（含失败回滚与重启重试）**、重启后查询。

## 旧数据卷迁移

早期版本把所有字段放在一张宽表 `frames` 里。启动时若检测到该结构（`PRAGMA
user_version = 0` 且 `frames` 含 `raw_word` 列），会在单个 `BEGIN IMMEDIATE`
事务中：

1. 按（固件号, 原始字）去重生成 `payloads`，同内容帧共用一行，最早解码时间随之保留；
2. 新建帧表并逐帧搬移 `id`、设备号、帧号、`received_at`、`decoded_at`，外键指向载荷；
3. 校验帧行数与外键完整性后，用新表替换旧表，置 `user_version = 1`。

迁移期间任一步失败都会整体 `ROLLBACK`：旧宽表与数据保持完整，下次启动自动重试。
新结构数据库再次启动只读到 `user_version = 1`，迁移幂等不重复执行。
