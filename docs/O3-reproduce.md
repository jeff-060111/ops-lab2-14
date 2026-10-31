# O3 部署与复现

在实验 Linux 主机的仓库根目录运行。沿用已安装的 Prometheus 3.13.4 和 node_exporter 1.12.1；只在回环地址监听，无需放通公网端口。服务绑核 2-3，采集与压测绑核 0-1；其他主机需按实际 CPU 拓扑调整脚本与第 7 格查询。

## 1. 检查配置

```bash
PROMTOOL=tools/prometheus-3.13.4.linux-amd64/promtool
"$PROMTOOL" check config deploy/prometheus.yml
"$PROMTOOL" test rules deploy/rules.test.yml
```

`deploy/prometheus.yml` 的 `rule_files: [rules.yml]` 相对配置文件所在目录解析。测试覆盖正常、慢成功、全部 503 且无成功序列、零流量、缺失目标、抓取失败、低流量和短尖峰。

## 2. 部署原生 Console

```bash
# 仅在仓库根目录的 consoles 尚不存在时建立链接；保留已有目录。
[ -e consoles ] || ln -s deploy/consoles consoles
```

本次运行中的 Prometheus 默认 `web.console.templates=consoles`，可直接使用该链接，无需重启。新启动可明确指定绝对或相对目录：

```bash
taskset -c 0-1 tools/prometheus \
  --config.file=deploy/prometheus.yml \
  --storage.tsdb.path=/dev/shm/lab2-prom \
  --web.listen-address=127.0.0.1:9090 \
  --web.console.templates=deploy/consoles
```

不要与已有 Prometheus 同时使用相同端口或 TSDB。已有实例使用 `kill -HUP <该实例PID>` 热加载规则；先通过 `ps` 确认 PID 和配置路径。本次未重启 Prometheus，保留了原有 TSDB。

## 3. 启动服务并运行实验

仅在 8080 没有已有 enrolld/其他实验时启动下列服务；不要覆盖别人的运行。数据写入 `data/o3`，不删除必做的 `data/enroll.log`。

```bash
mkdir -p data/o3
nohup taskset -c 2-3 bin/enrolld \
  -addr 127.0.0.1:8080 -pool 4 -db-ms 10 -work 200000 \
  -pool-timeout 1s -data data/o3 >data/o3/enrolld.log 2>&1 &
# 确认 node_exporter 已在 127.0.0.1:9100 运行；/api/v1/targets 两个目标均 up。
curl -fsS http://127.0.0.1:9090/api/v1/targets
curl -fsS http://127.0.0.1:9090/api/v1/rules

# 预测文件在 deploy/o3-prediction.md；先阅读并记录本次预期。
python3 scripts/o3-experiment.py runs/o3-repeat
python3 scripts/o3-snapshot.py runs/o3-repeat
```

总计约 13 分钟：100/s 300s → 600/s 120s → 100/s 360s。脚本使用真实读请求、每 2s 保存 `/api/v1/alerts`，首次 pending/firing/恢复分别保存原始 API JSON；观察不到完整状态链会非零退出。目录必须不存在，防止覆盖证据。运行中断时脚本停止本次 loadgen；服务与监控继续运行，SSH 连接需保持。

## 4. 查看与留存

在自己的电脑建立 SSH 隧道（将 `YOUR_HOST` 替换成实验主机）：

```bash
ssh -N -L 19091:127.0.0.1:9090 root@YOUR_HOST
```

打开 [实时九格看板](http://127.0.0.1:19091/consoles/enrolld.html) 或 [Prometheus 告警页](http://127.0.0.1:19091/alerts)。页面支持 15m/30m/1h 时间范围、每格展开 PromQL、手动刷新。可用 `?start=<Unix秒>&end=<Unix秒>` 固定历史区间；页首的实时告警状态仍取当前 API，不冒充历史状态。

`runs/o3-alert/dashboard.html` 是本次实验的单文件离线看板，直接用浏览器打开即可，不需要联网或 Prometheus。它嵌入 `dashboard-data.json` 的真实范围查询，页首标注“过载 firing 快照”，不能误解为当前仍在告警。每格的精确查询见 [O3-queries.md](O3-queries.md)。

TSDB 位于 `/dev/shm`，主机重启会丢失；正常 Prometheus 重启不等于删除该目录，但仍应先归档。本次完整归档含时间线、各阶段 CSV/metrics、规则快照、所有看板 API 查询结果及 SHA256 清单。验证：

```bash
cd runs/o3-alert
sha256sum -c SHA256SUMS
```

## 5. 规则含义与限制

SLO 为本次 O3 明确定义的请求型目标：30 天内 99% 的读请求成功且服务端耗时 ≤50ms。`burn=(1-good/total)/0.01`。1m 与 5m 的 burn 同时大于 14.4，短窗请求速率大于 1/s 且抓取正常，连续 30s 才 firing。无请求时比值没有定义；目标缺失、抓取失败需要另设可用性告警，本实验未把它伪装成延迟燃烧率告警。

14.4 倍表示在请求速率与预算周期平均速率相近、坏事件率持续且预算尚未消耗时，30 天预算约在 50 小时内耗尽。1m/5m 是便于现场演示的短窗口；不等于生产常用的 5m/1h 或完整多窗口策略，也不能宣称本实验已测出 30 天 SLO。实际部署应根据流量与值班策略选择窗口、阈值和低流量处理方式。

采用固定 50ms 桶直接计算达标比例，避免用插值 p99 反推 SLI。服务端没有记录请求到达前的内核排队、网络失败或客户端未发出的请求，故客户端 CSV 的 SLI 是独立对照，不要求与服务端一模一样。没有配置 Alertmanager，也没有向任何外部渠道发送通知。
