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

`scripts/table.sh runs/r1-<…> ok_rps error_rate p99_ms server_p99_ms server_mean_ms inflight_avg cpu_ms_per_req pool_in_use_avg pool_wait_p99_ms sched_wait_p99_ms svc_cpu_busy procs_running_avg`：

<粘贴表格>

最大吞吐量 <填写> 请求/秒，出现在第 <填写> 阶段；客户端 p99 首次超过 50 ms 是在第 <填写> 阶段（<填写> 请求/秒）。

### 4.3 CPU 消耗在何处

在约 0.8 倍上限的负载下采集 20 秒 CPU 剖析，`go tool pprof -top` 的前 10 行（同时保存在 `runs/<填写>/pprof-top.txt`）：

```text
<粘贴>
```

`main.spin` 占 <填写>%；按本次的吞吐量和 enrolld 消耗的 CPU 计算，每个请求 <填写> ms，其中 spin 约 <填写> ms，其余 <填写> ms 消耗在：<填写>

### 4.4 R2：调大连接池（预测在 R2 之前写）

| 项 | 值与算法 |
|---|---|
| 每个请求占用的 CPU（取 R1 哪一阶段的 `cpu_ms_per_req`，选取理由） | <填写> |
| 推算的 CPU 上限 C | <填写> |
| R2 的池大小 P2 = ceil(2 × C × 每次查询的秒数) | <填写> |
| R2 的到达率（`scripts/rates.sh C 0.3 0.6 0.8 0.9 1.0 1.1 1.25 1.5`） | <填写> |
| 预测：R2 中先达到上限的是 | <填写> |

`scripts/table.sh runs/r2-<…> ok_rps error_rate p99_ms server_p99_ms inflight_avg cpu_cores cpu_ms_per_req cpu_wait_threads sched_wait_p99_ms pool_in_use_avg svc_cpu_busy procs_running_avg cpu_psi_some`：

<粘贴表格>

最大吞吐量 <填写> 请求/秒；与预测的 C 相差：<填写>

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
| O4 迁移到小组项目 | <未做 / 仓库链接、测了哪个资源、结论> |

## 8. 成员分工

每位成员一行，只填写 GitHub 用户名，无需填写姓名；行数应与小组人数一致。

| GitHub 用户名 | 负责的部分 |
|---|---|
| <用户名> | <填写> |
