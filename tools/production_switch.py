#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot helper to switch the daily Actions workflow to portfolio-aware mode."""

from __future__ import annotations

import json
import os
from pathlib import Path


WORKFLOW = Path('.github/workflows/00-daily-analysis.yml')


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f'未找到 {label} 锚点，停止切换')
    return text.replace(old, new, 1)


def validate_private_portfolio() -> None:
    raw = (os.environ.get('PORTFOLIO_POSITIONS') or '').strip()
    if not raw:
        raise SystemExit('❌ PORTFOLIO_POSITIONS Secret 不可用')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit('❌ PORTFOLIO_POSITIONS 必须是 JSON object')

    codes: list[str] = []
    for key, value in payload.items():
        if str(key).startswith('_') or not isinstance(value, dict):
            continue
        qty = value.get('shares', value.get('quantity', 0))
        try:
            qty = float(qty or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0:
            codes.append(str(key).strip())

    if not codes:
        raise SystemExit('❌ Secret 中没有实际持仓')
    print(f'✅ 实际持仓代码（{len(codes)}只）: ' + ','.join(codes))


def main() -> None:
    text = WORKFLOW.read_text(encoding='utf-8')
    original = text

    stock_env = "          STOCK_LIST_CONFIG: ${{ vars.STOCK_LIST || secrets.STOCK_LIST }}"
    stock_env_new = stock_env + (
        "\n          # 私有持仓上下文（仅从 Actions Secret 注入）"
        "\n          PORTFOLIO_POSITIONS: ${{ secrets.PORTFOLIO_POSITIONS }}"
    )
    text = replace_once(text, stock_env, stock_env_new, 'STOCK_LIST_CONFIG')

    resolver = '''          if [ -n "${STOCK_LIST_CONFIG:-}" ]; then
            export STOCK_LIST="$STOCK_LIST_CONFIG"
          elif [ -z "${STOCK_LIST:-}" ]; then
            export STOCK_LIST="600519"
          fi'''
    resolver_new = resolver + '''

          # 正式持仓模式：股票列表以 PORTFOLIO_POSITIONS 中实际持有(shares/quantity>0)的标的为准。
          # 这里只输出股票代码；股数、成本、账户资产等私有字段不会写入控制台。
          if [ "$MODE" != "market-only" ]; then
            if [ -z "${PORTFOLIO_POSITIONS:-}" ]; then
              echo '❌ PORTFOLIO_POSITIONS Secret 未配置，正式持仓分析已中止'
              exit 1
            fi
            PORTFOLIO_STOCK_LIST="$(python -c 'import json,os; p=json.loads(os.environ["PORTFOLIO_POSITIONS"]); out=[]; [out.append(str(k).strip()) for k,v in p.items() if not str(k).startswith("_") and isinstance(v,dict) and float(v.get("shares",v.get("quantity",0)) or 0)>0]; print(",".join(out))')"
            if [ -z "$PORTFOLIO_STOCK_LIST" ]; then
              echo '❌ PORTFOLIO_POSITIONS 中没有可分析的实际持仓'
              exit 1
            fi
            export STOCK_LIST="$PORTFOLIO_STOCK_LIST"
          fi'''
    text = replace_once(text, resolver, resolver_new, 'STOCK_LIST 解析')

    text = replace_once(
        text,
        '            python main.py --no-market-review $FORCE_RUN_ARG',
        '            python portfolio_enhanced_main.py --no-market-review $FORCE_RUN_ARG',
        'stocks-only 执行命令',
    )
    text = replace_once(
        text,
        '            python main.py $FORCE_RUN_ARG',
        '            python portfolio_enhanced_main.py $FORCE_RUN_ARG',
        'full 执行命令',
    )

    upload_anchor = '''      - name: 上传分析报告
        uses: actions/upload-artifact@v6'''
    upload_new = '''      - name: 清理私有调试日志
        if: always()
        run: |
          # DEBUG 日志包含完整 prompt，可能带持仓成本/账户快照，禁止作为 artifact 上传。
          rm -f logs/stock_analysis_debug_*.log

      - name: 上传分析报告
        uses: actions/upload-artifact@v6'''
    text = replace_once(text, upload_anchor, upload_new, 'artifact 上传步骤')

    required = [
        "cron: '0 10 * * 1-5'",
        'PORTFOLIO_POSITIONS: ${{ secrets.PORTFOLIO_POSITIONS }}',
        'python portfolio_enhanced_main.py --no-market-review $FORCE_RUN_ARG',
        'python portfolio_enhanced_main.py $FORCE_RUN_ARG',
        'python main.py --market-review $FORCE_RUN_ARG',
        'rm -f logs/stock_analysis_debug_*.log',
    ]
    for marker in required:
        if marker not in text:
            raise SystemExit(f'切换后校验失败，缺少: {marker}')
    if text == original:
        raise SystemExit('文件没有发生变化，停止提交')

    WORKFLOW.write_text(text, encoding='utf-8')
    print('✅ 00-daily-analysis.yml 已切换为持仓增强正式模式')
    validate_private_portfolio()


if __name__ == '__main__':
    main()
