.PHONY: check check-paper check-live check-both portfolio api api-dev test-api init-db

ENV_FILE := gateway/.env
PAPER_PORT := 4002
LIVE_PORT := 4001
API_HOST := 127.0.0.1
API_PORT := 8000

# 检查网关可用性（TCP + API 握手）
check:
	$(MAKE) check-both

check-paper:
	@test -f "$(ENV_FILE)" || { echo "[FAIL] Missing $(ENV_FILE)"; exit 1; }
	@set -a; . "$(ENV_FILE)"; set +a; \
		PAPER_USER="$${IB_ACCOUNT_PAPER:-$$IB_ACCOUNT}"; \
		PAPER_PASS="$${IB_PASSWORD_PAPER:-$$IB_PASSWORD}"; \
		test -n "$$PAPER_USER" || { echo "[FAIL] Missing IB_ACCOUNT_PAPER (or IB_ACCOUNT) in $(ENV_FILE)"; exit 1; }; \
		test -n "$$PAPER_PASS" || { echo "[FAIL] Missing IB_PASSWORD_PAPER (or IB_PASSWORD) in $(ENV_FILE)"; exit 1; }; \
		IB_PORTS=$(PAPER_PORT) IB_API_PORT=$(PAPER_PORT) python3 scripts/check_ib_gateway.py

check-live:
	IB_PORTS=$(LIVE_PORT) IB_API_PORT=$(LIVE_PORT) python3 scripts/check_ib_gateway.py

check-both:
	$(MAKE) check-paper
	$(MAKE) check-live

# 查看当前资产组合
portfolio:
	IB_API_PORT=$(LIVE_PORT) python3 scripts/list_portfolio.py

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
