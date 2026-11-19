# 网关测量帧解码台

工厂数据员上传网关的两寄存器测量帧（CSV），平台按固件登记的字序把 8 位十六进制
原始字解成 IEEE 754 单精度读数，并按设备逐帧保留原文、状态与读数。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| CSV 列：设备号、帧号、固件号、8 位十六进制原始字 | `app/decoder.py:parse_csv`；支持中文/英文表头，字段可用引号包裹 |
| 每批至多 200 帧 | 解析阶段整批拒绝，不写入 |
| 同设备同帧号同内容重传沿用结果 | 不新增行、不重算，`received_at`/`decoded_at` 不变 |
| 同设备同帧号异内容整批拒绝 | 写入前全量比对，事务回滚，页面列出冲突行 |
| 已登记固件按高字在前 / 低字在前解码 | `high_first` / `low_first`，来自 `firmware` 表的不可变记录 |
| 非有限值（NaN、±Inf）标无效 | 读数为空，状态 `invalid`，保留原始位型 |
| 未知固件原帧标待解，不猜读数 | 状态 `pending`，`reading`、`bit_pattern` 均为空 |
| 登记字序后必须补解该固件全部待解帧 | 登记与补解在同一 `BEGIN IMMEDIATE` 事务中完成 |
| 登记与上传交错时不可见"已配置却仍待解" | 上传、补解、查询都在写事务/读快照中串行化，页面读同一快照 |
| 不能重复或错序解码 | 补解 `UPDATE ... WHERE status = 'pending'`；字序一经登记不可更改 |
| 失败保留原状态 | 任何异常都 `ROLLBACK`，整批不落地 |
| 重启后可查每帧原文、状态、有效读数 | SQLite 持久化于 `/data`，`/devices/<设备号>` 与 JSON API 可查 |
| docker compose 启动 | `Dockerfile` + `docker-compose.yml`，数据卷持久化 |

"内容"指原始字与固件号；设备号 + 帧号是帧的不可变身份。

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

- `/` 上传 CSV、登记固件字序、查看设备与固件列表
- `/devices/<设备号>` 该设备每帧的帧号、固件号、原始字、状态、读数、字序、解码位型、时间
- `/healthz` 健康检查（容器 HEALTHCHECK 使用）

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/upload` | 请求体为 CSV 文本（`text/csv`）或 `multipart/form-data` 的 `csv` 文件；成功 200，异内容整批拒绝 409 |
| POST | `/api/firmware` | JSON `{"fw_version": "...", "word_order": "high_first"\|"low_first"}`；新建 201，重复登记同字序 200，改字序 409 |
| GET | `/api/devices` | 设备列表与各设备帧数、待解数 |
| GET | `/api/devices/<设备号>` | 每帧原文、状态、有效读数、解码位型 |
| GET | `/api/firmwares` | 已登记固件及其字序 |

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
异内容整批拒绝、登记先后顺序、字序不可更改、补解只处理待解行、
重复登记不重复解码、并发上传与登记竞争、重启后查询。
