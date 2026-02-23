.PHONY: check check-paper check-live check-both portfolio api api-dev test-api init-db seed-sample

PAPER_PORT := 4002
LIVE_PORT := 4001
API_HOST := 127.0.0.1
API_PORT := 8000

# 检查网关可用性（TCP + API 握手，按 conf/app.toml 的 ib_gateway.trading_mode）
check:
	python3 scripts/check_ib_gateway.py

check-paper:
	TRADING_MODE=paper python3 scripts/check_ib_gateway.py

check-live:
	TRADING_MODE=live python3 scripts/check_ib_gateway.py

check-both:
	$(MAKE) check-paper
	$(MAKE) check-live

# 查看当前资产组合
portfolio:
	python3 scripts/list_portfolio.py

# 启动 FastAPI（生产参数可自行调整）
api:
	python3 -m uvicorn app.main:app --host $(API_HOST) --port $(API_PORT)

# 启动 FastAPI（开发模式）
api-dev:
	python3 -m uvicorn app.main:app --reload --host $(API_HOST) --port $(API_PORT)

# 后端 API 冒烟测试
test-api:
	python3 -m pytest app/tests/test_api_smoke.py -q

# 初始化 SQLite 表结构
init-db:
	python3 scripts/init_db.py

# 灌入干净样本数据（先清空业务数据，再写入 SMP-*）
seed-sample:
	python3 scripts/seed_sample_data.py
