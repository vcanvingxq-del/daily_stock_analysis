# 持仓盘中低频关键节点监控

`portfolio_intraday_monitor.py` 用于盘中轻量监控，与 18:00 的完整 AI 分析分离。它不会每轮调用 LLM，而是读取私有 `PORTFOLIO_POSITIONS`，对实际持仓执行关键价位、趋势线、20 日高低点和异常涨跌监控，只有真正跨越节点时才通过 PushPlus 推送。

## 默认规则

- 所有持仓：成本线上/下穿、20 日高点突破、20 日低点跌破。
- A 级持仓：额外监控 MA20，当日涨跌达到 ±4%。
- S 级高集中度持仓：额外监控 MA10、MA20，当日涨跌达到 ±3%。
- B 级普通持仓：当日涨跌达到 ±5%。
- 自动优先级：账户占比 >=25% 为 S；账户占比 >=10% 或浮亏 <=-15% 为 A；其余为 B。
- 同一事件、同一方向每天最多通知一次，避免横盘反复穿越造成刷屏。

`PORTFOLIO_POSITIONS` 中如果已有 `_account.total_assets`，监控器会用实时价格估算持仓占比，因此大仓位会自动获得更高优先级。

## 自定义关键位（可选）

可以把 `_alerts` 放进同一个私有 `PORTFOLIO_POSITIONS` Secret，不需要新建第二个持仓 Secret：

```json
{
  "_account": {"total_assets": 100000, "cash": 20000},
  "_alerts": {
    "000001": {
      "priority": "S",
      "levels": [
        {"price": 10.5, "direction": "below", "label": "风险防守位"},
        {"price": 11.8, "direction": "above", "label": "趋势转强位"}
      ]
    }
  },
  "000001": {"shares": 100, "cost": 11.0}
}
```

`_alerts` 以 `_` 开头，正式每日分析工作流不会把它误识别为持仓代码。

## 本机一次性验收

```bash
export PORTFOLIO_POSITIONS='...'
python portfolio_intraday_monitor.py --once --force --dry-run
```

第一次运行只建立价格基线，不会因为当前价格已经位于某个阈值一侧而补发历史提醒。之后只有真实跨越才会触发。

## PushPlus 微信通知

运行环境需要：

```bash
PUSHPLUS_TOKEN=你的Token
# 可选
PUSHPLUS_TOPIC=
```

不要把 Token 或持仓 JSON 写进仓库。建议放在服务器本地 `/opt/daily_stock_analysis/.env.monitor`，权限设为 `600`。

## 常驻运行

推荐小型常驻服务器，而不是 GitHub Actions 高频 cron。GitHub 定时任务会有延迟，并且每次 runner 都是临时环境，不适合保存“上一次价格/当天已通知事件”状态。

```bash
python portfolio_intraday_monitor.py --daemon --interval 120
```

默认只在北京时间工作日 09:25–11:35、12:55–15:05 轮询。状态保存在 `data/portfolio_intraday_monitor_state.json`，服务重启后仍可避免重复通知。

仓库提供 `deploy/portfolio-intraday-monitor.service.example` 作为 systemd 示例。

## 隐私

- `PORTFOLIO_POSITIONS` 原文不会主动打印。
- 默认日志只输出循环统计和错误，不列出完整持仓。
- 状态文件可能含股票代码、最近价格和触发记录，应仅保存在私有服务器，不要提交到公共仓库。
