## 1. 基本信息

| 项                           | 内容                                                                                                                                                                                                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 小组                          | 第 14 组                                                                                                                                                                                                                               |
| 仓库地址                        | <https://github.com/jeff-060111/ops-lab2-14>                                                                                                                                                                                         |
| 主机                          | 华为云 ECS，华东-上海一，4 vCPU / 8 GiB，按需计费                                                                                                                                                                                                   |
| CPU                         | `General Purpose Processor`（QEMU/KVM 虚拟 CPU，BIOS 报 `pc-i440fx-4.1 CPU @ 2.0GHz`）；4 vCPU；`lscpu -e`：CPU 0、1 → CORE 0，CPU 2、3 → CORE 1                                                                                                 |
| 内存                          | 7556 MB（`free -m` 的 total）                                                                                                                                                                                                           |
| 数据目录所在的盘                    | `/dev/vda1`，`ext4`，78.6 GB，挂载于 `/`；virtio-blk 云硬盘                                                                                                                                                                                    |
| 操作系统与内核                     | Ubuntu 24.04.4 LTS；6.8.0-136-generic                                                                                                                                                                                                 |
| Go、Prometheus、node_exporter | `go version go1.26.9 linux/amd64`；`prometheus, version 3.13.4 (branch: HEAD, revision: 007a9e9ba628f815ce83e1ba9f29b97981996c3c)`；`node_exporter, version 1.12.1 (branch: HEAD, revision: 6044da783597cc3b57aef7580ddcdcff58a4ee99)` |
| 分核                          | enrolld：2-3；loadgen、Prometheus、node_exporter：0-1                                                                                                                                                                                     |

`lscpu` 报 `Thread(s) per core: 2`、`Core(s) per socket: 2`，而 `lscpu -e` 显示 4 个逻辑 CPU 只对应 2 个物理核（CPU 0、1 同属 CORE 0，CPU 2、3 同属 CORE 1）。两个超线程共享同一物理核的执行单元，因此把 enrolld 绑在 2-3、把压测与采集工具绑在 0-1，两者落在不同物理核上，不会互相抢执行单元。这也是后面所有命令都用 `GEN_CPUS=0-1`、`taskset -c 2-3` 的原因。

## 2. 任务 0：基线

`bin/enrolld -bench-fsync 2000` 连续运行三次的输出：

```text
fsync 次数 2000，总耗时 2.41 s，数据目录 data
fsync 耗时（毫秒）：mean 1.192  p50 1.101  p90 1.363  p99 3.071  max 4.904
按平均值推算，一个写者串行 fsync 每秒最多约 839 次（= 1000 / 1.192）
fsync 次数 2000，总耗时 2.38 s，数据目录 data
fsync 耗时（毫秒）：mean 1.176  p50 1.096  p90 1.347  p99 2.824  max 4.221
按平均值推算，一个写者串行 fsync 每秒最多约 850 次（= 1000 / 1.176）
fsync 次数 2000，总耗时 2.35 s，数据目录 data
fsync 耗时（毫秒）：mean 1.161  p50 1.079  p90 1.312  p99 3.186  max 4.922
按平均值推算，一个写者串行 fsync 每秒最多约 861 次（= 1000 / 1.161）
```

按平均值计算，单个写者串行 fsync 每秒最多约 **850** 次（三次 mean 为 1.192、1.176、1.161 ms，取平均 1.176 ms，1000 ÷ 1.176 ≈ 850）。三次之间相差很小：极差 0.031 ms，相对差 2.6%，折算成吞吐是 839 与 861 之间相差 22 次。三次呈单调下降（越跑越快），是大块写入逐渐稳定、页缓存与日志分配预热的结果，不是随机抖动。

但尾部并不稳定：p99 为 2.82～3.19 ms、max 为 4.22～4.92 ms，约为均值的 2.4 倍与 3.6～4.2 倍。**均值稳定、尾部有抖动**，这是云盘经虚拟化层、写请求须落盘确认时的典型特征，也说明用均值推算串行吞吐是可行的（长期吞吐由均值决定），但单次请求的等待时间会有数倍差别。

另有一次 `-bench-fsync 5000`（总耗时 6.14 s，mean 1.218 ms，约 821 次/秒）与一次在两条观察命令同时运行下的 `-bench-fsync 5000`（6.22 s，mean 1.234 ms，约 810 次/秒），与上述三次处于同一量级（相差 3～5%），可视为独立复现；正式数值以三次 2000 次的结果为准。

空载和运行 fsync 测试时各工具的读数（各观察 10 秒以上，填写典型值）：

| 工具与列                                   | 空载                      | 运行 `-bench-fsync` 时                          | 该列的含义                                                                                                       |
| -------------------------------------- | ----------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `vmstat -w 1`：r、b                      | 0～1、0                   | 0、1                                          | `r` 是可运行队列长度（正在运行或等待上 CPU 的进程数）；`b` 是处于不可中断睡眠（D 状态）的进程数，在 Linux 上通常就是在等块设备 I/O                              |
| `vmstat -w 1`：us、sy、id、wa、st           | 0～1、0～1、99～100、0、0      | 1～2、2～4、72～74、23、0                           | `us` 用户态时间占比；`sy` 内核态占比；`id` 空闲占比；`wa` 是 CPU 空闲但有未完成 I/O 的时间占比；`st` 是被 hypervisor 抢走的时间（steal）              |
| `iostat -dx 1 <盘>`：w/s、wkB/s           | 0～8、0～40 kB/s           | 2347～2517、约 12.9～13.7 MB/s（12856～13740 kB/s） | `w/s` 是每秒合并后发送给设备的写请求数；`wkB/s` 是每秒写入的字节数                                                                    |
| `iostat -dx 1 <盘>`：w_await、f/s、f_await | 0～1.00 ms、0～2、0～0.05 ms | 0.38～0.41 ms、1565～1678、0.04 ms               | `w_await` 是写请求从发出到完成的平均耗时（含排队）；`f/s` 是每秒 flush 请求数，fsync/fdatasync 最终都变成设备的 flush；`f_await` 是 flush 请求的平均耗时 |
| `iostat -dx 1 <盘>`：aqu-sz、%util        | 0、0～0.20%               | 1.01～1.02、94.5～95.7%                         | `aqu-sz` 是平均在途（已发出未完成）请求数，即设备侧的队列长度；`%util` 是设备处于忙碌状态的时间占比                                                  |

说明：`vmstat` 的第一次输出是开机以来的平均值（`cs` 只有 5，而后续为 2000～3000），`iostat` 的第一块同样是开机以来的平均值（`w/s` 2.39、`%util` 0.05%），均按要求剔除。带载段首尾各有一行属于过渡状态：`vmstat` 的第一行 `sy=84%、cs=146171` 是 `enrolld` 进程启动瞬间建立文件与首次分配带来的系统调用高峰，随后立刻回落到 `sy=2～4%`；`iostat` 的最后一块 `w/s=1588、%util=61.8%` 是压测结束后的收尾。表中取的是中间六组稳定值。

运行 fsync 测试时，CPU **不繁忙**。四个 vCPU 中 `us` 只有 1～2%、`sy` 2～4%，`id` 仍有 72～74% —— 约四分之三的时间无事可做。

存在等待的证据有三处，彼此印证：

1. `wa` 从 0 升到 **23%**：CPU 空闲，但有未完成的 I/O，这段空转在等盘。
2. `vmstat` 的 `b` 从 0 变为 **1**：有一个进程进入不可中断睡眠（D 状态），它在等块设备返回。
3. `iostat` 的 `aqu-sz` 从 0 升到 **1.01～1.02**、`%util` 从 0.1% 升到 **94.5～95.7%**：设备侧平均有 1 个请求在途，且设备几乎全程忙碌。

三者指向同一结论：瓶颈在磁盘的持久化路径，不在 CPU。

同时有两组数值值得记下来，第 6 节问题 3 会用到：

- **设备级延迟远小于应用级**：`w_await` 只有 0.38～0.41 ms、`f_await` 只有 0.04 ms，而应用层测到的 fsync 是 1.176 ms。差额主要在文件系统与日志提交路径，不在设备本身。
- **一次 fsync 大约对应 3 次写、2 次 flush**：带载时 `w/s` 约 2440、`f/s` 约 1620，而 fsync 速率是 5000 ÷ 6.22 ≈ 804 次/秒，2440 ÷ 804 ≈ 3.0，1620 ÷ 804 ≈ 2.0。


## 3. 任务 1：指标流水线

两个抓取目标的状态（`curl -s 127.0.0.1:9090/api/v1/targets` 中两处 `"health"` 的值）：`up`、`up`（重启后第一次查询时 enrolld 目标曾显示 `unknown`，因为 node_exporter 刚启动、Prometheus 尚未完成首轮抓取，其抓取间隔为 5 秒；下一轮即为 `up`）。

本组编写的四条查询（与 `deploy/queries-red.txt` 一致）：

```text
server_rps          sum(rate(http_requests_total{route="${route}"}[${window}]))
server_error_rate   (sum(rate(http_requests_total{route="${route}",code=~"5.."}[${window}])) or vector(0)) / sum(rate(http_requests_total{route="${route}"}[${window}]))
server_p99_ms       1000 * histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{route="${route}"}[${window}])))
server_slo_good     sum(rate(http_request_duration_seconds_bucket{route="${route}",code="200",le="${slo}"}[${window}])) / sum(rate(http_request_duration_seconds_count{route="${route}"}[${window}]))
```

四处写法上的理由：

- `server_rps` 用 `rate` 而不是直接取 `http_requests_total`，因为后者是 counter，只增不减；`sum` 把各 `code` 的序列合并成一条。
- `server_error_rate` 的分子加 `or vector(0)`：没有 5xx 时 `code=~"5.."` 匹配不到任何序列，比值会变成空值而不是 0。分母是全部请求，所以 499（客户端主动断开，非 5xx）只进分母、不进分子。
- `server_p99_ms` 必须先 `sum by (le)` 再进 `histogram_quantile`。直方图带 `code` 标签，每个状态码各有一套桶；不先聚合就会出现“200 的 p99”和“503 的 p99”两条序列，算出来的不是整体分位数。
- `server_slo_good` 的阈值 `${slo}` 恰好是直方图的一个桶上界（读路径 0.05 = 50 ms），所以可以直接读累计桶，不必线性插值。分子用 `code="200"` 与 `le` 两个条件同时限定，分母用 `_count`（全部请求，不分状态码）。

`runs/t1-light` 的数据（`scripts/table.sh runs/t1-light ok_rps mean_ms p99_ms server_rps server_mean_ms server_p99_ms cpu_ms_per_req db_query_mean_ms`）：

| step | target_rps | ok_rps | mean_ms | p99_ms | server_rps | server_mean_ms | server_p99_ms | cpu_ms_per_req | db_query_mean_ms |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 0 | 0.0 | - | - | - | - | - | - | NaN |
| 2 | 50 | 50.5 | 11.65 | 12.65 | 51.75 | 10.8732 | 19.9 | 0.637082 | 10.457 |
| 3 | 100 | 97.8 | 11.64 | 13.77 | 95.4167 | 10.9362 | 19.9 | 0.59397 | 10.4886 |
| all | - | 74.2 | 11.64 | 12.77 | - | - | - | - | - |

从这次实验读出：每次查询实际占用连接 **10.49** ms（`db_query_mean_ms`，第 3 阶段）；每个请求消耗的 CPU **0.594** ms（`cpu_ms_per_req`，第 3 阶段）。

两点说明，供第 6 节使用：

- `db_query_mean_ms` 比启动参数 `-db-ms 10` 大约 0.49 ms。查询的实现是“占住连接后睡 10 ms”，但 goroutine 的唤醒要经过定时器与调度器，实际占用时间比设定的睡眠时间略长；`-pool` 的容量必须按实测的占用时间算，不能按参数算。
- `server_p99_ms` 在两阶段都是 19.9 ms，而客户端用最近秩法算出的 p99 是 12.65 ms 与 13.77 ms，两者相差 6～7 ms。19.9 几乎正好等于直方图 `(10 ms, 20 ms]` 这个桶的上界：本实验的直方图桶上界为 1、2、5、10、20、50、100、200、500、1000、2000、5000 ms，读路径的耗时集中在 10.4 ms 左右，**全部落进同一个 `(10, 20]` 桶**，`histogram_quantile` 只能在桶内线性插值，于是把 0.99 分位算到了桶上界附近。这说明：**服务端的 p99 精度受桶粒度限制，桶太宽时只能当作“落在哪一段”来读**。客户端与服务端 p99 的这一差距不是服务端更慢，而是两者的计算方法不同——客户端对原始样本排序取第 ⌈p·n⌉ 个，服务端只能在累计桶之间插值。

## 4. 任务 2：读路径

### 4.1 预测（R1 之前写）

| 资源 | 容量 | 每个请求占用（数据来源） | 推算的上限（请求/秒） |
|---|---|---|---|
| 连接池 | 4 个连接 | 10.49 ms（`runs/t1-light/red.csv` 第 3 阶段 `db_query_mean_ms`） | 4 ÷ 0.01049 ≈ 381 |
| CPU | enrolld 的 2 个核 | 0.594 ms（`runs/t1-light/red.csv` 第 3 阶段 `cpu_ms_per_req`） | 2 ÷ 0.000594 ≈ 3367 |

先达到上限的是：**连接池**。两个上限相差 8.8 倍（3367 ÷ 381），读路径在用到不到三成的 CPU 时就会被 4 个连接卡住。

预计从多少请求/秒起，客户端 p99 超过 50 ms：**约 305 请求/秒（连接池上限的 0.8 倍）**，理由：并发请求争用 4 个连接，排队近似于 4 个服务台的排队系统；池内等待的 p99 在利用率 0.8 附近已到十几毫秒，叠加每次查询 10.49 ms 的占用时间后总延迟接近 25～30 ms，到 0.9 倍（343 请求/秒）时等待 p99 通常越过 40 ms，总延迟跨过 50 ms。因此预测第 5 阶段（305）为首次接近阈值、第 6 阶段（343）稳定超过。

R1 的到达率（`scripts/rates.sh 381` 的输出）：76,152,229,267,305,343,381,419,457


### 4.2 R1：`-pool 4`

`scripts/table.sh runs/r1-pool4-v2 ok_rps error_rate p99_ms server_p99_ms server_mean_ms inflight_avg cpu_ms_per_req pool_in_use_avg pool_wait_p99_ms sched_wait_p99_ms svc_cpu_busy procs_running_avg`：

| step | target_rps | ok_rps | error_rate | p99_ms | server_p99_ms | server_mean_ms | inflight_avg | cpu_ms_per_req | pool_in_use_avg | pool_wait_p99_ms | sched_wait_p99_ms | svc_cpu_busy | procs_running_avg |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 76 | 78.2 | 0.0000 | 12.78 | 19.9 | 10.9034 | 0.96 | 0.61392 | 0.92 | 0.998381 | 0.474439 | 0.02775 | 1.4 |
| 2 | 152 | 150.3 | 0.0000 | 17.80 | 19.9301 | 11.1097 | 1.4 | 0.58036 | 1.36 | 7.51885 | 0.475376 | 0.046 | 1.4 |
| 3 | 229 | 229.3 | 0.0000 | 22.10 | 31.8267 | 11.8134 | 2.08 | 0.560762 | 1.92 | 12.4206 | 0.472 | 0.06325 | 2 |
| 4 | 267 | 272.2 | 0.0000 | 30.72 | 46.647 | 13.46 | 3.12 | 0.547668 | 2.56 | 19.8319 | 0.475927 | 0.07075 | 3 |
| 5 | 305 | 308.4 | 0.0000 | 44.42 | 48.8456 | 15.7974 | 5.48 | 0.53982 | 3.44 | 44.0957 | 0.472476 | 0.08 | 1.8 |
| 6 | 343 | 339.9 | 0.0000 | 52.98 | 69.9044 | 19.5801 | 6.96 | 0.534371 | 3.68 | 48.4362 | 0.471261 | 0.0865 | 1.8 |
| 7 | 381 | 382.9 | 0.0000 | 480.58 | 495.056 | 224.673 | 86.2 | 0.522725 | 3.96 | 494.978 | 0.475087 | 0.095 | 2.6 |
| 8 | 419 | 386.2 | 0.0730 | 1011.73 | 1980.38 | 994.793 | 410.56 | 0.514528 | 3.84 | 1887.35 | 0.471642 | 0.1055 | 2.6 |
| 9 | 457 | 380.6 | 0.1654 | 1012.12 | 1986.87 | 1002.16 | 458.04 | 0.516051 | 4 | 1945.45 | 0.472948 | 0.113 | 1.2 |
| all | - | 280.9 | 0.0402 | 1011.19 | - | - | - | - | - | - | - | - | - |

最大成功吞吐量为 **386.2 请求/秒**，出现在第 8 阶段（目标 419 请求/秒），但此时错误率已达 **7.30%**，不能将其视为满足服务质量目标的容量。无错误时的最高阶段为第 7 阶段，成功吞吐量 382.9 请求/秒，但客户端 p99 已达 480.58 ms。以客户端 p99 不超过 50 ms 为条件，最后满足条件的是第 5 阶段（目标 305 请求/秒、成功吞吐量 308.4 请求/秒）；首次超过 50 ms 是第 **6** 阶段（目标 **343 请求/秒**、p99 **52.98 ms**），比第 4.1 节预测的约 305 请求/秒晚一个阶段。

第 7 阶段的 `pool_in_use_avg` 为 **3.96/4**、`pool_wait_p99_ms` 为 **494.978 ms**，而服务核 `svc_cpu_busy` 仅 **0.095**；第 8 阶段继续加压时成功吞吐量只到 386.2 请求/秒，并开始出现 503。连接池接近满载、池等待快速增长且 CPU 仍有余量，说明 R1 先达到上限的是连接池。`steps.csv` 的发送滞后 p99 为 1.05～1.07 ms、压测机 CPU 为 1%～5%，本次客户端结果未见发送能力不足的警告。

### 4.3 CPU 消耗在何处

约 0.8 倍连接池上限对应 R1 第 5 阶段（目标 305 请求/秒）。该阶段 `runs/r1-pool4-v2/red.csv` 的 `cpu_ms_per_req` 为 **0.53982 ms/请求**，`cpu_cores` 为 **0.167367**。在相同目标到达率 305 请求/秒下额外加压 40 秒，中间采集 20 秒 CPU 剖析；`loadgen_exit=0`，pprof 共采得 2.97 秒 CPU 样本。`go tool pprof -top` 的前 10 行保存在 `runs/r1-pool4-v2/pprof-top.txt`：

```text
      flat  flat%   sum%        cum   cum%
     2.49s 83.84% 83.84%      2.49s 83.84%  main.spin (inline)
     0.16s  5.39% 89.23%      0.16s  5.39%  internal/runtime/syscall/linux.Syscall6
     0.06s  2.02% 91.25%      0.06s  2.02%  runtime.futex
     0.03s  1.01% 92.26%      0.04s  1.35%  encoding/json.appendString[go.shape.string]
     0.01s  0.34% 92.59%      0.06s  2.02%  bufio.(*Reader).fill
     0.01s  0.34% 92.93%      2.82s 94.95%  net/http.(*conn).serve
     0.01s  0.34% 93.27%      0.07s  2.36%  runtime.netpoll
     0.01s  0.34% 93.60%      0.05s  1.68%  syscall.Read (inline)
         0     0% 93.60%      0.06s  2.02%  bufio.(*Reader).Peek
         0     0% 93.60%      0.13s  4.38%  bufio.(*Writer).Flush
```

`main.spin` 的 flat CPU 样本占 **83.84%**。将这一采样占比应用于同等负载下 R1 第 5 阶段实测的 **0.53982 ms CPU/请求**，估计每请求约 **0.453 ms** 用于 spin，其余约 **0.087 ms** 用于 HTTP 处理、系统调用、Go 运行时调度及 JSON 编码等。pprof 中 `linux.Syscall6` 占 5.39%、`runtime.futex` 占 2.02%、`encoding/json.appendString` 占 1.01%；这些是 flat 占比，`net/http.(*conn).serve` 的 94.95% 是包含 `main.spin` 等子函数的累计占比，不能再加到上述各项中。CPU 时间拆分是用一次同负载采样比例乘以 R1 阶段均值的估计，不是逐请求的直接测量。

### 4.4 R2：调大连接池（预测在 R2 之前写）

| 项 | 值与算法 |
|---|---|
| 每个请求占用的 CPU（取 R1 哪一阶段的 `cpu_ms_per_req`，选取理由） | `runs/r1-pool4-v2/red.csv` 第 6 阶段的 **0.534371 ms/请求**。该阶段目标为 343 请求/秒，`ok_rps` 为 339.9、错误率为 0，尚未达到吞吐上限；在这些条件下它是负载最高的阶段。 |
| 推算的 CPU 上限 C | 2 核 ÷ 0.000534371 秒/请求 ≈ **3743 请求/秒** |
| R2 的池大小 P2 = ceil(2 × C × 每次查询的秒数) | 第 6 阶段 `db_query_mean_ms` 为 10.4778 ms；P2 = ceil(2 × 3743 × 0.0104778) = **79**。对应连接池理论上限约 79 ÷ 0.0104778 ≈ 7540 请求/秒。 |
| R2 的到达率（`scripts/rates.sh C 0.3 0.6 0.8 0.9 1.0 1.1 1.25 1.5`） | `scripts/rates.sh 3743 0.3 0.6 0.8 0.9 1.0 1.1 1.25 1.5` → **1123,2246,2994,3369,3743,4117,4679,5615** |
| 预测：R2 中先达到上限的是 | **CPU**。池大小 79 的理论上限约为所估 CPU 上限的 2 倍，预计吞吐和延迟的拐点出现在 CPU 接近满载时。 |

`scripts/table.sh runs/r2-pool79 ok_rps error_rate p99_ms server_p99_ms inflight_avg cpu_cores cpu_ms_per_req cpu_wait_threads sched_wait_p99_ms pool_in_use_avg svc_cpu_busy procs_running_avg cpu_psi_some`：

| step | target_rps | ok_rps | error_rate | p99_ms | server_p99_ms | inflight_avg | cpu_cores | cpu_ms_per_req | cpu_wait_threads | sched_wait_p99_ms | pool_in_use_avg | svc_cpu_busy | procs_running_avg | cpu_psi_some |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1123 | 1131.6 | 0.0000 | 12.59 | 19.9 | 12.36 | 0.534639 | 0.470271 | 0.177137 | 0.488842 | 12.24 | 0.262 | 3.6 | 0.0841638 |
| 2 | 2246 | 2241.1 | 0.0000 | 12.92 | 19.9 | 24.04 | 0.987842 | 0.440108 | 0.300349 | 0.841054 | 23.56 | 0.492 | 2.2 | 0.136886 |
| 3 | 2994 | 2986.0 | 0.0000 | 13.64 | 19.9 | 31 | 1.28193 | 0.430309 | 0.289582 | 1.5314 | 30.56 | 0.638 | 3.4 | 0.132821 |
| 4 | 3369 | 3362.8 | 0.0000 | 14.18 | 19.9 | 34.2 | 1.42897 | 0.424793 | 0.197489 | 1.88777 | 33.68 | 0.70525 | 3 | 0.097713 |
| 5 | 3743 | 3744.5 | 0.0000 | 14.93 | 19.9 | 39.44 | 1.577 | 0.421029 | 0.14741 | 3.06431 | 38.6 | 0.78275 | 5 | 0.0747932 |
| 6 | 4117 | 4135.3 | 0.0000 | 16.51 | 19.9 | 43.84 | 1.73628 | 0.418979 | 0.0801694 | 4.47539 | 42.92 | 0.863 | 3.6 | 0.0474239 |
| 7 | 4679 | 4689.8 | 0.0000 | 38.67 | 19.9145 | 49.84 | 1.94457 | 0.41493 | 0.0337331 | 19.75 | 48.88 | 0.9715 | 3.4 | 0.0254589 |
| 8 | 5615 | 676.5 | 0.8794 | 1997.76 | 1978.71 | 3165 | 1.9929 | 0.443723 | 0.0226162 | 1000 | 67.2381 | 0.9995 | 5.2 | 0.0331422 |
| all | - | 2870.9 | 0.1769 | 1721.73 | - | - | - | - | - | - | - | - | - | - |

最大成功吞吐量为第 **7** 阶段的 **4689.8 请求/秒**，比预测的 CPU 上限 C = 3743 请求/秒高 **946.8 请求/秒（25.3%）**。第 7 阶段服务消耗 1.94457 个核，服务核忙碌比例为 0.9715，Go 调度等待 p99 升至 19.75 ms；连接池平均只占 48.88/79，池等待 p99 约 0.99 ms（`runs/r2-pool79/red.csv`），因此先接近上限的是 CPU，而非连接池。第 8 阶段服务 CPU 达 1.9929 核、忙碌比例 0.9995，Go 调度等待 p99 达 1000 ms，成功吞吐量跌至 676.5 请求/秒，错误率升至 87.94%，表明已经严重过载。

预测偏低的一个主要原因是每请求 CPU 随负载下降：R1 第 6 阶段用于预测的值为 0.534371 ms，而 R2 第 7 阶段实测为 0.41493 ms，低约 **22.4%**。按后者和 2 核换算的 CPU 上限约为 4820 请求/秒，与第 7 阶段实测成功吞吐量更接近。第 8 阶段 `steps.csv` 报发送滞后 p99 为 23.87 ms，故其客户端 p99 = 1997.76 ms 不用于精确定位上限；CPU 满载、Go 调度等待和吞吐下降仍能证明服务已过载。

## 5. 任务 3：写路径

### 5.1 预测：`-sync each`（W1 之前写）

| 项 | 值与算法 |
|---|---|
| fsync 平均耗时（任务 0） | <填写> ms |
| 推算的最大 TPS | <填写> |
| 达到上限时 enrolld 大约使用几个核（写路径没有 spin，按 4.3 节拆分出的“spin 以外的开销”估计每个写请求的 CPU） | <填写> |
| W1 的到达率（`scripts/rates.sh <最大 TPS> 0.2 0.4 0.6 0.8 0.9 1.0 1.1 1.2`） | <填写> |

### 5.2 W1：`-sync each`

`scripts/table.sh runs/w1-<…> ok_rps error_rate p99_ms server_p99_ms cpu_cores fsync_per_s fsync_mean_ms batch_mean svc_cpu_busy cpu_iowait disk_w_per_s disk_w_await_ms disk_f_per_s disk_f_await_ms disk_aqu_sz disk_util`：

<粘贴表格>

### 5.3 预测：`-sync group`（W2 之前写）

在 W1 达到上限时的到达率下，改为组提交后：吞吐量 <填写>；平均每次 fsync 处理 <填写> 条记录；客户端 p99 约 <填写>。理由：<填写>

W2 的到达率：<填写>

### 5.4 W2：`-sync group`

（列与 5.2 相同）

<粘贴表格>

### 5.5 W3：`-sync interval`

（列与 5.2 相同）

<粘贴表格>

## 6. 分析问题

每题先列出所用数据（引用上面的表或 `runs/` 中的文件），再写结论，并说明结论的成立条件。每题 300～600 字。

**问题 1：上限由什么决定。** 按 L-07 第 25 页资源账的格式，为 enrolld 的读路径编制一份资源账：资源、每个请求占用（实测）、容量、推算的上限、实测的最大吞吐量、饱和观测项。预测与实测相差多少，差异来自何处？`cpu_ms_per_req` 随负载如何变化，原因是什么？这对“在低负载下测量每请求开销，再推算上限”的做法意味着什么？`db_query_mean_ms` 为什么大于 `-db-ms`？以“p99 ≤ 50 ms”为目标，R1 能承受的最大阶段相当于上限的多少比例？与 L-07 第 6 页“容量 = 上限 × 0.7”比较，为什么尚未达到上限延迟就已上升？R2 调大连接池后，瓶颈转移到了哪个资源？

<填写>

**问题 2：利用率不等于饱和。** R1 达到上限时，enrolld 所在核的 CPU 利用率是多少？哪个数值最先表明资源已经饱和？R2 中 CPU 利用率接近 100% 时，内核层的 `procs_running_avg`、`cpu_psi_some`、`cpu_wait_threads` 和 Go 运行时层的 `sched_wait_p99_ms` 分别如何变化？为什么内核层几乎观察不到排队，排队发生在哪里？这与 L-06 第 16、17 页 Tomcat 的线程池有何相同之处？最后用 Little's Law 核对：从 R1 选取三个阶段，用 `server_rps × server_mean_ms` 计算在途请求数，并与 `inflight_avg` 比较；W 为什么必须用平均值而不能用 p99？哪个阶段不一致，原因是什么？

<填写>

**问题 3：持久性以延迟为代价。** W1 实测的最大 TPS 与 1000 ÷ fsync 平均耗时相比如何？W1 达到上限时 CPU、内存、磁盘的指标各是多少，哪几列表明磁盘饱和？应用测得的 `fsync_mean_ms` 与 `disk_w_await_ms`、`disk_f_await_ms` 为什么不同？一次 fsync 大约对应几次写和几次 flush？W1 与 W2 在 `disk_util` 相近时吞吐量相差多少，`%util` 接近 100% 能否说明磁盘已经饱和？组提交的 `batch_mean` 如何随负载变化，低负载时是否有收益，本组的预测是否正确？W3 快了多少，代价是什么？选课记录应采用哪一种方式，在什么条件下 interval 可以接受？

<填写>

**问题 4：延迟数值的测量位置与方法。** 同一阶段中，客户端与服务端的平均值、p99 各相差多少？列出只计入客户端的时间组成。找出一个服务端 p99 大于客户端 p99 的阶段，并解释原因（L-08 第 10 页）。比较服务端的 `server_slo_good` 与客户端的 `slo_good`；若存在过载阶段，二者为何相差很大？SLI 应在何处测量（L-08 第 19 页）？用 R1 的 `steps.csv`，将各阶段的 p99 求平均，与 `all` 行的 p99 比较，说明百分位数为什么不能平均，以及正确的合并方法。最后计算错误预算：SLO 为“30 天内 99% 的读请求在 50 ms 内成功返回”，若 R1 中第一个不达标阶段的负载持续下去，燃烧率是多少，30 天的预算多久耗尽（L-08 第 21 页）？

<填写>

## 7. 选做记录

| 选做 | 完成情况与证据 |
|---|---|
| O1 内存与 GC | <未做 / 预测、`runs/` 目录、结论> |
| O2 网络 | <未做 / 两台主机的规格与拓扑、`runs/` 或日志、结论> |
| O3 告警与看板 | <未做 / 规则文件、触发记录、看板说明> |
| O4 迁移到小组项目 | 已完成：PaperMate 加入 RED 并测量连接池上限；单连接池最高已测达标目标 325/s，四连接池 458/s。见下方 7.1 节、[完整 O4 报告](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/docs/O4.md)及[PR #96](https://github.com/li-yuelang/SE26Project-05/pull/96)。 |

### 7.1 O4：将 RED 与容量测量迁移到 PaperMate

**项目与版本：** [小组项目 SE26Project-05](https://github.com/li-yuelang/SE26Project-05)；[项目 PR #96](https://github.com/li-yuelang/SE26Project-05/pull/96) 已 squash 合并，合并提交为 [`add6a84f7a402d79f899779dbc4746d2fdcd52ed`](https://github.com/li-yuelang/SE26Project-05/commit/add6a84f7a402d79f899779dbc4746d2fdcd52ed)。以下报告和数据链接均固定到该提交，避免后续 main 更新改变评分依据。[完整 O4 报告](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/docs/O4.md)，以及[全部 O4 原始证据](https://github.com/li-yuelang/SE26Project-05/tree/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/)。

**对象与方法：** 为真实 FastAPI 后端加入 RED 三项指标，压测 `GET /api/papers/discover?page=1&page_size=20`，保留 Bearer 认证和真实 PostgreSQL 查询；使用固定 1,000 条模拟论文、不调用大模型/arXiv、不添加人工延迟。主机为 4 vCPU/8 GiB，4 vCPU 对应两个物理核的 SMT；API 单 worker 绑 2,3，数据库、Go 压测器及监控绑 0,1。泊松开环到达，每阶段 30 秒；池 max_overflow=0、获取超时 0.5 秒，对比 pool_size=1 与 4。O4 自定目标为“成功率至少 99%，成功请求客户端 p99 不超过 200 ms”，与必做读路径的 50 ms 阈值分别记录。

**RED 迁移：** R 用 `papermate_http_requests_total{method,route,code}` 的 rate；E 用 5xx/全部请求；D 用 `papermate_http_request_duration_seconds` 直方图查询平均值和 p99，另有在途请求指标。标签使用路由模板，不包含论文 ID、查询参数或 Token。实现见 [HTTP 埋点](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/backend/app/core/metrics.py)、[连接池埋点](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/backend/app/core/db_metrics.py) 和 [PromQL 查询文件](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/deploy/o4/queries-red.txt)；抓取配置位于 [prometheus.yml](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/deploy/o4/prometheus.yml)。

**结果摘要（原始客户端 CSV）：**

| 池大小 | 目标到达率（请求/秒） | 成功吞吐（请求/秒） | 错误率 | 客户端 p99（ms） | 数据出处 |
|---:|---:|---:|---:|---:|---|
| 1 | 325 | 327.4 | 0.00% | 173.84 | [r1-refine-pool1-01 阶段 2](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r1-refine-pool1-01/steps.csv) |
| 1 | 340 | 339.8 | 0.03% | 316.72 | [r1-refine-pool1-01 阶段 3](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r1-refine-pool1-01/steps.csv) |
| 1 | 353 | 335.1 | 6.25% | 503.82 | [r1-go-pool1-01 阶段 3](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r1-go-pool1-01/steps.csv) |
| 4 | 353 | 356.2 | 0.00% | 50.39 | [r2-go-pool4-01 阶段 2](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r2-go-pool4-01/steps.csv) |
| 4 | 458 | 457.0 | 0.01% | 144.14 | [r2-go-pool4-01 阶段 3](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r2-go-pool4-01/steps.csv) |
| 4 | 599 | 322.8 | 46.43% | 570.68 | [r2-go-pool4-01 阶段 4](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r2-go-pool4-01/steps.csv) |

单连接池最高已测达标目标点为 **325 请求/秒**；340 请求/秒时 p99 已超过 200 ms，达标边界落在 325～340 目标请求/秒之间，不能把 325 当精确长期容量。池扩大为 4 后，最高已测达标目标点为 **458 请求/秒**，比 325 提高约 41%；继续加压至 599/s 不达标。353/s 时池从 1 扩到 4，错误率从 6.25% 降为 0、p99 从 503.82 ms 降为 50.39 ms，支持单连接池为初始瓶颈。R1 的 599/s 阶段成功返回不足一半，原计划的 740/s 阶段被工具自动停止。

**按 L-07 第 25 页格式的一行资源账：**

| 资源 | 峰值需求 | 容量 | 利用率 | 饱和观测项 |
|---|---|---|---|---|
| 单 worker 的 SQLAlchemy 连接池，P=1 | R1 已测最高目标 599/s × 低负载持有时间 H=2.6927 ms ≈ 1.613 个连接的理想占用需求 | 1 个连接；P/H 近似 371/s；实测约 340～353 目标/s 时明显排队/超时，最高已测达标目标 325/s | 599/s 阶段平均占用 1.0/1=100%；模型需求/容量约 161%，两者不同 | 平均获取中调用约 297.5 个；503 为 12215/18062；客户端 p99 567.27 ms；扩大池后同负载改善 |

资源账的 H 来自 [Go 低负载基线 server.json 阶段 2](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/b2-go-pool1-01/server.json)；占用与获取中调用来自 [R1 server.json 阶段 5](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r1-go-pool1-01/server.json)；503 次数、请求数与客户端 p99 来自 [R1 steps.csv 阶段 5](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/r1-go-pool1-01/steps.csv)。其中 `acquiring` 包括排队、建连和 pre-ping，不能称为纯等待者；H 是 checkout 到 checkin 的时间，简单 P/H 未包括 checkout 前的槽位预留开销，只是近似。

**预测与实测：** 低负载 CPU 为 2.8360 ms/request，初步估计 CPU 上限 352.6/s、池上限 371.4/s，原预测 CPU 略早达到上限；实测先出现单连接池瓶颈，原预测未修改。在未过载的 247/s 阶段每请求 CPU 已降至 2.0872 ms，低负载外推偏保守；请求开销随负载变化、连接预留与泊松突发也影响边界。R2 前据该阶段重新预测 CPU 约 479.1/s，实测四连接池在 599/s 时 API CPU 约 0.996 核，扩大池无法带来四倍吞吐，CPU 饱和与池等待可以同时存在。事前预测文件：[R1](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/PREDICTION-R1.json)、[R2](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/PREDICTION-R2.json)、[临界点补测](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/PREDICTION-R1-REFINE.json)。

**证据口径与复现：** 每次实验目录保留 `meta.json`、`steps.csv`、`server.csv/json`、`prometheus-step-*.json`（完整查询与响应）、原始 metrics、vmstat/mpstat/iostat/pidstat 日志和 SHA256 清单。89 个原始证据文件在项目提交后再次验证哈希一致；测量时源码快照按文本展开保存在[measured-source](https://github.com/li-yuelang/SE26Project-05/tree/add6a84f7a402d79f899779dbc4746d2fdcd52ed/runs/o4/measured-source/)，不是声称当时已经存在本次合并提交。测试数据和复现工具见 [seed_o4.py](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/backend/scripts/seed_o4.py)、[o4_go_run.py](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/backend/scripts/o4_go_run.py) 与 [完整 O4 报告](https://github.com/li-yuelang/SE26Project-05/blob/add6a84f7a402d79f899779dbc4746d2fdcd52ed/docs/O4.md)。原始 O4 数据保留在小组项目的固定提交中，以上链接可直接追溯；本实验仓库的必做数据继续使用本仓库 `runs/`。

客户端按计划发送时刻到响应体读完计时，只统计成功请求；服务端为中间件入口到最后响应体发送，p99 采用直方图插值。客户端 CSV 是整段，服务端查询剔除首 5 秒和末 1 秒，因此过载时数值不能逐项等同；宽桶也会影响服务端 p99。以上结论限于该主机、单 worker、固定数据和短阶段测量，不代表完整项目的 LLM、Celery 或抓取性能。组员准备现场复现时可在同一主机使用报告中的 O4 独立服务与参数，不与必做实验同时压测。

## 8. 成员分工

每位成员一行，只填写 GitHub 用户名，无需填写姓名；行数应与小组人数一致。

| GitHub 用户名 | 负责的部分 |
|---|---|
| <用户名> | <填写> |
