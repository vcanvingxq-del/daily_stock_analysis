# Sequoia-X 集成说明

本仓库通过独立 GitHub Actions workflow 集成第三方项目 [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X)，不把其 Python 依赖直接合并到 daily_stock_analysis 主依赖中，以降低依赖冲突和主分析链路回归风险。

## 工作流

文件：`.github/workflows/sequoia-x.yml`

- 上游版本固定到提交 `444c0db69ff36b46ef2b22ab265051d60c16029d`。
- Python 使用 3.11。
- 工作日北京时间 19:15 自动运行日常模式。
- SQLite 数据目录 `.sequoia-x/data` 使用 GitHub Actions cache 在运行间恢复。
- 每次运行额外上传 7 天保留期的数据目录 artifact，便于排障或手动恢复。

## 首次初始化

合并本集成后，进入 GitHub Actions 的 `Sequoia-X A股选股` workflow，选择 `Run workflow`，将 `mode` 设为 `backfill` 并运行。

首次回填会生成 `data/sequoia_v2.db`。后续定时任务会优先从 Actions cache 恢复该数据库，再执行增量同步和选股。

如果日常模式未找到数据库，workflow 会直接失败并提示先执行 `backfill`，避免误以为空库增量运行成功。

## Secrets

必须配置：

- `FEISHU_WEBHOOK_URL`：默认飞书机器人 Webhook。仓库现有每日分析 workflow 已使用同名 Secret，可复用。

可选策略独立 Webhook：

- `SEQUOIA_WEBHOOK_MA_VOLUME`
- `SEQUOIA_WEBHOOK_TURTLE`
- `SEQUOIA_WEBHOOK_FLAG`
- `SEQUOIA_WEBHOOK_SHAKEOUT`
- `SEQUOIA_WEBHOOK_LIMIT_DOWN`
- `SEQUOIA_WEBHOOK_RPS`
- `SEQUOIA_WEBHOOK_PRIVATE_PLACEMENT`

未配置策略独立 Webhook 时，Sequoia-X 会回退到默认 `FEISHU_WEBHOOK_URL`。

## 更新上游版本

不要把 workflow 的 `ref` 改为 `master`。升级时应先检查上游变更和依赖，再把 `ref` 更新到明确 commit SHA，并重新执行一次 `backfill` / `daily` 验证。

## 回滚

删除 `.github/workflows/sequoia-x.yml` 即可停止该集成。该 workflow 不修改 daily_stock_analysis 的主分析入口、依赖文件或 `.github/workflows/00-daily-analysis.yml`。
