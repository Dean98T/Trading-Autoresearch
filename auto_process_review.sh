#!/bin/bash
cd /home/trading-llm-auto-research
if [ -f logs/research_maintenance.flag ]; then
  printf '[%s] maintenance active, skip scheduled process review\n' "$(date '+%F %T')" >> logs/auto_process_review.log
  exit 0
fi
source .env
exec .venv/bin/python3 auto_process_review.py >> logs/auto_process_review.log 2>&1
