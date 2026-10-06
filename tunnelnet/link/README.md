# 链路在线监测与降级（link monitoring & degraded running）

管廊采集设备靠心跳判断在线状态。链路整体不可用时，系统进入**降级运行**，明确
暂停哪些联动；设备状态、链路状态和每一次状态迁移都落盘，控制台与审计读同一份数据。

## 1. 两条固定策略（必须先定，避免实现歧义）

| 决策点 | 选定策略 | 误判 / 漏判取舍 |
| --- | --- | --- |
| 短暂抖动是否立刻判离线 | **不立刻判**。连续 `miss_threshold`（默认 3）次心跳丢失才判 `OFFLINE`；只丢 1 次先标 `JITTER`，留痕可见但**不触发任何控制动作** | 立即判离线在管廊有线/无线抖动下误报率高，还会反复启停联动。先抖动后离线把误判代价压到“只告警不动作”，代价是最坏检测延迟 = `miss_threshold × interval`（默认 30s），两个参数都可按链路配置 |
| 恢复是否自动回正常 | **设备层自动恢复**（需连续 `recover_hits`（默认 2）次心跳，带回滞防 flapping）；**链路层退出降级需人工确认**：设备全在线后链路先转 `RECOVERING`，稳定观察 `recover_stable_seconds`（默认 30s）后由人工 `confirm` 才回 `ONLINE` | 设备心跳是实时信号，自动恢复免维护；但“退出降级”会重新放开被暂停的联动，属安全敏感动作，人工确认可避免链路反复横跳时联动被瞬间重新触发。紧急情况下可用 `force=true` 跳过观察窗 |

## 2. 状态机

设备（由心跳驱动）：

```
UNKNOWN --first beat--> ONLINE
ONLINE  --miss 1-----> JITTER   (只记录，不动作)
JITTER  --miss>=N----> OFFLINE  (N = miss_threshold)
OFFLINE/JITTER --连续 recover_hits 次心跳--> ONLINE   (回滞)
```

链路（由其下设备聚合驱动，离线设备比例 `offline / total >= offline_ratio`
且至少 1 台离线时触发；默认 0.5）：

```
ONLINE --离线比例达标------------> DEGRADED   (暂停普通联动，安全联动保持布防)
DEGRADED --全部设备在线----------> RECOVERING (联动仍暂停，等待稳定+确认)
RECOVERING --观察窗到 + 人工确认-> ONLINE     (恢复普通联动，幂等)
RECOVERING --观察期内再次离线----> DEGRADED   (复发)
ONLINE/RECOVERING --人工 rollback> DEGRADED  (回滚一次恢复，幂等)
```

- `UNKNOWN`（登记后从未上报心跳）不参与降级聚合，但控制台健康检查标 `warn`，
  杜绝“设备其实没连上，控制台却全绿”。
- 每次在线↔离线翻转累计 `flaps`，便于评估链路质量。

## 3. 降级时哪些联动被暂停

联动分两类（见 `gate.py`）：

- **NORMAL（降级时暂停）**：`access.release` 开门、`env.alarm` 环境告警路由、
  `vent.run` 通风、`drain.pump` 排水泵、`patrol.checkin` 巡检签到、`energy.meter` 电能联动。
- **SAFETY（永不暂停）**：`fire.suppression` 消防抑制（数据不可信时保护动作保持布防）、
  `audit.record` 审计记录。

`/api/cycle` 在降级时跳过被暂停的普通联动，安全联动照常执行，并在响应里用
`skipped_linkages` 明确列出被暂停的联动；`/api/link` 的 `paused` 字段是同一来源。

## 4. 时序依赖、回滚与幂等

- **先落盘后动作**：进入 `DEGRADED/ONLINE` 前先把链路状态持久化，再调用真实控制器
  暂停/恢复联动（与 `access`“先存审批再开门”同一模式）。崩溃在“判定/动作”之间时，
  重启后按磁盘状态重新暂停，结论不丢。
- **可补偿 saga**：暂停/恢复联动失败时执行反向动作补偿并回滚已落盘的状态，
  迁移事件不写出。中途部分成功也会逐个反向恢复。
- **恢复幂等**：已 `ONLINE` 再 `confirm` 返回成功但 `restored=[]`；
  已 `DEGRADED` 再 `rollback` 返回 `rolled_back=false`。重复恢复不会重复启停联动。
- 每次进入降级自增 `episode`，审计/事件都带 episode，便于区分不同降级轮次。

## 5. 落盘与“同一份数据”

存在既有原子写 JSON `Store` 下（写临时文件再 `os.replace`）：

- `link/<id>.json`：链路状态与策略；
- `device/<id>.json`：设备心跳状态；
- `link_event/<seq>.json`：**只追加**的状态迁移流水（seq 单调，重启后可按序重放）。

控制台链路视图读 `link_event`；同一条 `Transition` 通过 `AuditTransitionSink`
镜像进控制台共用的内存审计 `AuditRegistry`，因此 `/api/audit*` 与
`/api/link/events` 看到的是同一批事件，不存在两份相互矛盾的状态。

## 6. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/link` | 系统 mode、各链路、设备汇总、被暂停联动 |
| GET | `/api/link/links?id=` | 链路及其设备明细 |
| GET | `/api/link/devices?link=` | 设备心跳状态与汇总 |
| GET | `/api/link/events?scope=` | 落盘状态迁移流水 |
| GET | `/api/link/linkages?key=` | 联动门状态，`allowed` 判断某联动是否放行 |
| POST | `/api/link/register` | `link_id` 建链路或 `device_id`+`on` 登记设备 |
| POST | `/api/link/heartbeat` | `{device_id, at?}` 上报心跳（无 at 用服务时钟） |
| POST | `/api/link/sweep` | 推进时钟老化心跳并重新评估（后台 sweeper 自动调用） |
| POST | `/api/link/recover/confirm` | 稳定后人工确认恢复（`force=true` 跳过观察窗） |
| POST | `/api/link/recover/rollback` | 回滚一次恢复，重新进入降级 |

生产环境由 `tunnelnet/link/sweeper.py` 的守护线程按
`--sweep-interval`（默认 5s）自动 `sweep`；测试用可注入的 `Clock`
（`runtime.link_clock`）确定性推进时间。

## 7. 关键默认参数

`interval=10s`，`miss_threshold=3`（最坏 30s 判离线），
`recover_hits=2`（连续 2 次心跳恢复），`offline_ratio=0.5`，
`recover_stable_seconds=30s`，后台扫描 5s。均可按链路在登记时覆盖。
