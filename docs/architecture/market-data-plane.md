# 行情数据平面（Market Data Plane）架构

## 目标
把「取行情」与「LLM 调用」解耦。用户请求路径里不再有行情网络调用，只读本地热数据。
- 降 API 量：一处集中拉取，多处消费；多用户持同一只股只拉一次（市场数据全局共享）。
- 秒开：侧栏 OKR / 绩效 / 工具读数命中本地热数据。
- 事件驱动：本地数据异动 → 自动触发分析/报告（P3）。

**非目标**：不解决 LLM 本身慢（中转 14s/次）；不做 tick 级（目标 30–60s 快照粒度）。

## 三层
- **L1 数据源适配**（`core/tools/market_tools.py`）：klineshare→东财→新浪/腾讯，多源回退 + 负缓存 +（P4）熔断。只被 L2 调用。
- **L2 数据平面**（`core/market_plane.py`）：单一后台 loop。按交易时段自适应频率轮询 → Snapshot（内存+落盘）+ Derived（每租户组合/约束/健康，每 tick 预算）+（P3）Change Detector→事件总线。唯一碰行情 API 的地方。
- **L3 消费层**：Agent 工具 / 侧栏 / 绩效 / 定时任务 / Web API。只读 L2，读穿透（命中即返，过期返旧值+异步刷新，缺失才回源）。

## 关键决策（已拍板）
- 轮询：盘中 45s，盘后/非交易日暂停（用收盘快照）。
- 存储：内存为主 + 定期落盘 `data/market/snapshot.json`（重启不冷启）。
- 多租户：市场数据全局单份（按 symbol 键）；派生数据按 uid。
- 数据自带 `as_of`/`source`/`stale`，杜绝旧数当实时。
- P3 异动触发 LLM：仅止损线 + 大幅波动（±5%）等重事件，带去重/冷却/预算闸，默认进铃铛+按用户推送设置。

## 分期
- P1 骨架：Snapshot + Poller（持仓/大盘，交易时段自适应）+ 读穿透；工具/侧栏改读 Store。
- P2 Derived 预算：portfolio/constraints/health 每 tick 算好缓存。
- P3 事件总线 + 异动自动分析。
- P4 L1 熔断 + 东财批量快照。

## 现有代码映射（增量非重写）
- 多源取价 → 下沉为 L1，只被 L2 调用。
- 分散缓存 → 收敛为 L2 Snapshot。
- `portfolio_compute`/`constraints`/`portfolio_health` → L2 Derived，每 tick 预算。
- `alerts.poll_loop`+`portfolio_health.scan_and_notify` → 合并进 L2 Change Detector（P3）。
- 工具/接口签名不变，仅数据来源由「现查」变「读本地」。
