# 链路在线监测与降级运行

管廊采集设备的心跳在这里被判定为在线状态，链路整体不可用时系统进入降级、
暂停指定联动；恢复经过稳定窗口后自动回到正常模式，支持人工挂起、失败回滚和
重复操作幂等。所有状态与事件落盘，控制台页面和审计接口读同一份文件。

## 1. 两条定死的策略

### 1.1 离线判定：连续 N 次心跳丢失（不是丢一次即离线）

设备状态机：

```
ONLINE ──丢1次心跳──▶ SUSPECT ──连续丢满 miss_limit 次──▶ OFFLINE
  ▲                    │ 收到任意新心跳                    │
  └────────────────────┴──────────────────────────────────┘
```

* `SUSPECT`：一个心跳周期未收到心跳，立即可见的预警，但**不触发**任何控制
  动作（不降级、不暂停联动）。
* `OFFLINE`：连续 `miss_limit`（默认 3）次心跳丢失才确认，确认延迟上限为
  `(miss_limit-1) × heartbeat_period`。
* `JITTER`：在 `flap_window`（默认 60s）内出现 ≥ `flap_limit`（默认 3）次
  "健康→丢心跳"边沿即判抖动；抖动期间按**不可用**计（抖动链路的数据不可信），
  连续健康满一个 `flap_window` 后自动退出。
* 收到重复/回退序号的心跳报文不改变状态（防重放）。

取舍：丢一次即离线，漏判低但误判高，会造成联动频繁暂停/恢复，现场总线下的
一次瞬断就停排水比短暂漏数危害更大；连续 N 次确认把误判降下来，代价是有界
的确认延迟，`SUSPECT` 承担"快速可见"。对安全关键设备可配置 `critical=true`，
其 OFFLINE/JITTER 不需要达到比例阈值，一票否决整条链路。

链路整体判定（`tunnelnet/link/policy.py: link_availability`）：
不可用设备（OFFLINE+JITTER）比例 ≥ `offline_ratio`（默认 0.34），或任一关键
设备不可用，则链路 `down`。SUSPECT 不计入不可用。

### 1.2 恢复上线：自动恢复（带稳定窗口），人工可以挂起

链路模式机：

```
DEGRADED ──链路恢复可用──▶ RECOVERING ──连续 stable_ticks 次健康──▶ NORMAL
   ▲                          │                                      │
   └──── 恢复期间再掉线 / resume 被拒（fencing token+1 回滚）────────┘
```

* 默认不需要人工确认：无人值守管廊里，人工确认会导致系统长期停在降级态。
* `POST /api/links/hold`：人工挂起，即使链路健康也保持降级（检修窗口用）；
  `POST /api/links/release` 解除。
* `POST /api/links/recover`：人工确认入口，与自动恢复走**完全相同**的稳定、
  回滚、幂等规则（只是顺手解除 hold）。
* 故障安全联动（`fail_safe=true`，如气体/火灾告警）**任何模式都不暂停**。

## 2. 时序依赖：判离线与控制动作的顺序

每次评估严格按下面顺序，前一步落盘成功后才做下一步（见
`monitor.py` 模块文档与 `_evaluate_link/_reconcile_linkages`）：

1. 从磁盘读设备状态，评估出新的设备状态与边沿事件；
2. **持久化**设备状态和事件；
3. 判定链路可用性与下一运行模式，**持久化**模式转换（先记
   `mode_degraded`，后产生命令）；
4. 按模式对账每个联动的期望门状态，把 pause/resume 命令写入 outbox；
5. 向执行器下发命令，携带本链路当前的 fencing token；
6. 记录命令结果；resume 失败则整体回滚 DEGRADED 并 bump token。

因此控制动作永远不会先于"离线判定"；进程在任意一步崩溃，磁盘都准确描述了
"打算做什么"，重跑 tick 只补做未完成的动作。

## 3. 幂等与回滚

* **命令幂等键**：`{link}:{linkage}:{action}:t{token}`。同一降级周期内重复
  评估/重复调用恢复，不会重复下发 pause/resume。
* **fencing token**：每次进入降级（含回滚）token +1。恢复期间又掉线，或
  resume 命令被执行器拒绝，token 递增，所有在途旧命令在执行器处被
  `StaleCommand` 拒绝——旧恢复无法把联动重新打开。
* **resume 失败回滚**：任一非故障安全联动恢复失败，整个链路立即回滚
  DEGRADED，其余联动的期望状态在同一轮内重新对齐为 paused；episode 重新
  打开，故障排除后再次走完整稳定窗口才能 NORMAL。
* **pause 失败**：保持 DEGRADED，门状态记 `pause_failed` 持续重试，不进入
  恢复（停不下来的联动不能被当作已安全暂停）。
* **episode**：一次降级从开 episode 到所有联动恢复 active 才关闭；
  NORMAL 下重复 tick 不产生新 episode、不产生新命令。

## 4. 落盘与"同一份数据"

全部写入复用现有原子写的 `Store`（临时文件 + rename）：

| kind | 内容 |
| --- | --- |
| `link-config` | 每条链路的判定阈值 |
| `link-device` / `link-device-state` | 设备配置 / 设备实时状态 |
| `link-state` | 链路模式、可用性、hold、fencing token、稳定计数 |
| `link-linkage` / `link-linkage-state` | 联动配置 / 期望与实际门状态 |
| `link-command` | pause/resume outbox（幂等键、token、结果、尝试次数）|
| `link-episode` | 降级周期开/闭 |
| `link-event` | 只追加审计事件（设备、链路、联动、episode、命令）|

`GET /api/links`（控制台）与 `GET /api/links/audit`（审计）都通过
`LinkRepository` 读这些文档，不存在第二份内存真相；进程重启后从磁盘恢复
模式、门状态、token 与 episode，再按对账结果补做未完成动作。

## 5. HTTP 接口

| 方法/路径 | 用途 |
| --- | --- |
| `GET  /api/links` | 全部链路、设备、联动门的实时投影 |
| `GET  /api/links/policy` | 当前生效的离线/恢复策略文字说明 |
| `GET  /api/links/audit?entity_type=&entity_id=&limit=` | 链路审计事件 |
| `GET  /api/links/commands?link_id=` | pause/resume 命令及结果 |
| `GET  /api/links/episodes?link_id=` | 降级周期 |
| `POST /api/links/register` | 注册链路（阈值可配）|
| `POST /api/links/devices/register` | 注册采集设备（可标 critical）|
| `POST /api/links/linkages/register` | 注册受控联动（可标 fail_safe）|
| `POST /api/links/heartbeat` | 上报心跳 `{device_id, seq, at?}` |
| `POST /api/links/tick` | 用时钟超时评估一条或全部链路 |
| `POST /api/links/hold` / `release` / `recover` | 人工挂起/解除/确认恢复 |

生产启动时 `LinkScanner` 守护线程按 `--link-scan-interval`（默认 5s）周期
执行 tick，因此即使没有任何请求，静默设备也会被自动判定离线并降级。
控制台页面：`/links`（5s 自动刷新，可挂起/解除/立即评估）。

降级时 `/api/cycle` 的响应明确给出 `paused_linkages` 与 `degraded`：
排水自动联动（`drain-auto`）、通风调度（`vent-auto`）被跳过，火灾/气体告警
（`gas-alarm`，fail-safe）照常执行。

## 6. 关键文件

* `tunnelnet/link/model.py` — 状态、命令、事件、episode 数据结构
* `tunnelnet/link/policy.py` — 设备/链路判定纯函数（误判/漏判取舍所在）
* `tunnelnet/link/repository.py` — 落盘与同一份读取视图
* `tunnelnet/link/monitor.py` — 模式机、对账、fencing、幂等、回滚
* `tunnelnet/link/gate.py` — 业务动作执行前的门控查询
* `tunnelnet/link/scanner.py` — 后台超时扫描
* `tunnelnet/console/link_handlers.py` — HTTP 接口
* `tests/test_links.py`、`tests/test_links_console.py` — 24+9 个行为/端到端测试
