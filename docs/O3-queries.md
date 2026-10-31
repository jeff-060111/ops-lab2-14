# O3 九格看板：逐格 PromQL

布局依据 L-08 第 29 页：业务 → 服务 RED → 资源 USE。实时入口是 Prometheus 的 `/consoles/enrolld.html`；查询源文件为 `deploy/consoles/enrolld-queries.json`。本页与查询源对应，每个子查询都有独立单位，p50/p99 在同图展示。

样例没有真实用户会话、端到端选课完成率、MySQL、Kafka，因此不能原样复刻课程平台的业务指标。下方明确列出替代项与缺口。所有请求指标仅统计读/提交业务接口，不混入 /metrics、/config、健康检查。

## 业务：请求是否成功完成

### 01 · 读请求达标率 / 错误预算

以读请求成功且 ≤50ms 的比例替代完整选课完成率。99% 目标；1m、5m 燃烧率均 >14.4 持续 30s 告警。

**1-1-1 · 读 SLI · 1m（%）**

```promql
100 * (1 - enrolld:read_slo_burn:ratio1m{job="enrolld"} * 0.01)
```

**1-1-2 · 燃烧率 · 1m（倍）**

```promql
enrolld:read_slo_burn:ratio1m{job="enrolld"}
```

**1-1-3 · 燃烧率 · 5m（倍）**

```promql
enrolld:read_slo_burn:ratio5m{job="enrolld"}
```

### 02 · 请求成功率 / 提交成功率

HTTP 200 成功率。提交仅排除 400 输入拒绝；无提交时显示无数据。其他非 200 仍计为失败。

**1-2-1 · 读请求成功率（%）**

```promql
100 * ((sum(rate(http_requests_total{job="enrolld",route="/api/courses/{id}",code="200"}[1m])) or (0 * sum(rate(http_requests_total{job="enrolld",route="/api/courses/{id}"}[1m])))) / sum(rate(http_requests_total{job="enrolld",route="/api/courses/{id}"}[1m])))
```

**1-2-2 · 提交成功率（%）**

```promql
100 * ((sum(rate(http_requests_total{job="enrolld",route="/api/enroll",code="200"}[1m])) or (0 * sum(rate(http_requests_total{job="enrolld",route="/api/enroll",code!="400"}[1m])))) / sum(rate(http_requests_total{job="enrolld",route="/api/enroll",code!="400"}[1m])))
```

### 03 · 在线人数：未埋点

样例没有用户会话和在线人数指标。以下仅作连接侧核对：主机全部 TCP 连接与 conntrack 条目均不等于人数。

**1-3-1 · 主机 TCP 已建立连接（个）**

```promql
node_netstat_Tcp_CurrEstab{job="node"}
```

**1-3-2 · 主机 conntrack 条目（个）**

```promql
node_nf_conntrack_entries{job="node"}
```

## 服务 · RED：谁在等待

### 04 · 按接口 QPS / 错误率

按路由模板聚合。5xx 为服务错误；499 属于客户端中断，只进入全部请求分母。

**2-1-1 · 接口 QPS（req/s）**

```promql
sum by (route) (rate(http_requests_total{job="enrolld",route=~"/api/courses/.*|/api/enroll"}[1m]))
```

**2-1-2 · 接口 5xx（%）**

```promql
100 * ((sum by (route) (rate(http_requests_total{job="enrolld",route=~"/api/courses/.*|/api/enroll",code=~"5.."}[1m])) or on (route) (0 * sum by (route) (rate(http_requests_total{job="enrolld",route=~"/api/courses/.*|/api/enroll"}[1m])))) / sum by (route) (rate(http_requests_total{job="enrolld",route=~"/api/courses/.*|/api/enroll"}[1m])))
```

### 05 · 按接口 p50 / p99

服务端入口到返回的耗时，含全部状态码。先合并桶再算分位数；插值误差取决于桶宽，不能与客户端 p99 等同。

**2-2-1 · p50 / p99（ms）**

```promql
label_replace((1000 * histogram_quantile(0.50, sum by (route, le) (rate(http_request_duration_seconds_bucket{job="enrolld",route=~"/api/courses/.*|/api/enroll"}[1m])))), "quantile", "p50", "", "") or label_replace((1000 * histogram_quantile(0.99, sum by (route, le) (rate(http_request_duration_seconds_bucket{job="enrolld",route=~"/api/courses/.*|/api/enroll"}[1m])))), "quantile", "p99", "", "")
```

### 06 · 在途请求 / 连接池等待

inflight 是在途请求，不是在线人数。样例未使用 Kafka；池等待含获取失败前的等待。

**2-3-1 · 在途请求（个）**

```promql
http_inflight_requests{job="enrolld",route=~"/api/courses/.*|/api/enroll"}
```

**2-3-2 · 池占用（%）**

```promql
100 * db_pool_in_use{job="enrolld"} / db_pool_size{job="enrolld"}
```

**2-3-3 · 池等待 p99（ms）**

```promql
1000 * histogram_quantile(0.99, sum by (le) (rate(db_pool_wait_seconds_bucket{job="enrolld"}[1m])))
```

## 资源 · USE：在等待什么

### 07 · CPU / 内存

服务绑核 2、3；busy 不含 idle/iowait。运行队列、steal、可用内存为整机指标，包含其他进程。

**3-1-1 · 服务核 busy（%）**

```promql
100 * sum(rate(node_cpu_seconds_total{job="node",cpu=~"2|3",mode!~"idle|iowait"}[1m])) / count(node_cpu_seconds_total{job="node",cpu=~"2|3",mode="idle"})
```

**3-1-2 · 主机运行队列（线程）**

```promql
node_procs_running{job="node"}
```

**3-1-3 · 主机 steal（%）**

```promql
100 * avg(rate(node_cpu_seconds_total{job="node",mode="steal"}[1m]))
```

**3-1-4 · 可用内存（GiB）**

```promql
node_memory_MemAvailable_bytes{job="node"} / 1073741824
```

### 08 · 磁盘 / 持久化

vda 为数据目录所在真实云盘。读实验不主动写入；磁盘计数可能来自其他进程。样例没有 MySQL buffer pool。

**3-2-1 · 写 await（ms）**

```promql
1000 * rate(node_disk_write_time_seconds_total{job="node",device="vda"}[1m]) / rate(node_disk_writes_completed_total{job="node",device="vda"}[1m])
```

**3-2-2 · 磁盘忙碌（%）**

```promql
100 * rate(node_disk_io_time_seconds_total{job="node",device="vda"}[1m])
```

**3-2-3 · 磁盘 aqu-sz（个）**

```promql
rate(node_disk_io_time_weighted_seconds_total{job="node",device="vda"}[1m])
```

### 09 · 网络 / 丢弃

读压测走 lo；eth0 为主机外部流量。ListenOverflows 显示增量速率，不把历史累计值误判为当前故障。

**3-3-1 · eth0 发送（Mbit/s）**

```promql
8 * rate(node_network_transmit_bytes_total{job="node",device="eth0"}[1m]) / 1000000
```

**3-3-2 · lo 发送（Mbit/s）**

```promql
8 * rate(node_network_transmit_bytes_total{job="node",device="lo"}[1m]) / 1000000
```

**3-3-3 · 监听队列溢出（次/s）**

```promql
rate(node_netstat_TcpExt_ListenOverflows{job="node"}[1m])
```

## 口径与判读

- `rate` 窗口为 1m；燃烧率的长窗为 5m；页面每 10s 刷新，查询步长 5s。瞬时 gauge 图不是阶段均值。
- 直方图合并所有状态码并按 `le` 保留桶，再计算 p50/p99；不能平均各实例的分位数。
- 慢成功与非 200 都消耗读 SLO 预算；服务 RED 错误率仅看 5xx，二者含义不同。
- 没有 5xx 的已存在路由使用同一路由的总请求序列补零；没有任何请求、没有提交、或分母为零时保留无数据。
- 提交成功率仅排除本样例明确返回的 400 输入拒绝；不能把全部 4xx 都当业务拒绝。其他非 200 均不算成功。服务端未观测到的网络失败仍需由客户端/网关另测。
- 队列满时先看池占用、池等待与在途；CPU 高低只作为原因证据。磁盘和网络指标为整机共享计数，不能归因于 enrolld 单一进程。
- `eth0` 不包含回环读负载；因此同时展示 `lo`。TCP/conntrack 包含 SSH、监控及其他服务，不等于在线用户。
- 资源格仅进入看板。唯一告警是 `EnrolldReadSLOBurnRateHigh`；未配置 Alertmanager 或外部通知渠道。

燃烧率定义与生产窗口取舍可参照 [Google SRE Workbook](https://sre.google/workbook/alerting-on-slos/)；规则持续时间语义见 [Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)。Console 页面使用自带脚本和 SVG，不依赖 Prometheus 2.x 的旧前端库，见 [Console templates](https://prometheus.io/docs/visualization/consoles/)。
