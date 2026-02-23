.PHONY: check check-paper check-live check-both portfolio api api-dev test-api init-db seed-sample

PAPER_PORT := 4002
LIVE_PORT := 4001
API_HOST := 127.0.0.1
API_PORT := 8000
PYTHON ?= python
PYTHON_OK := $(shell $(PYTHON) -c 'import sys; print(int(sys.version_info >= (3, 11)))' 2>/dev/null)

ifeq ($(PYTHON_OK),0)
$(error Python 3.11+ is required. Current: $(shell $(PYTHON) -V 2>/dev/null || echo "not found"). Use: make PYTHON=/path/to/python3.11 <target>)
endif

# 检查网关可用性（TCP + API 握手，按 conf/app.toml 的 ib_gateway.trading_mode）
check:
	$(PYTHON) scripts/check_ib_gateway.py

check-paper:
	TRADING_MODE=paper $(PYTHON) scripts/check_ib_gateway.py

check-live:
	TRADING_MODE=live $(PYTHON) scripts/check_ib_gateway.py

check-both:
	$(MAKE) check-paper
	$(MAKE) check-live

# 查看当前资产组合
portfolio:
	$(PYTHON) scripts/list_portfolio.py

# 启动 FastAPI（生产参数可自行调整）
api:
	$(PYTHON) -m uvicorn app.main:app --host $(API_HOST) --port $(API_PORT)

# 启动 FastAPI（开发模式）
api-dev:
	$(PYTHON) -m uvicorn app.main:app --reload --host $(API_HOST) --port $(API_PORT)

# 后端 API 冒烟测试
test-api:
	$(PYTHON) -m pytest app/tests/test_api_smoke.py -q

# 初始化 SQLite 表结构
init-db:
	$(PYTHON) scripts/init_db.py

# 灌入干净样本数据（先清空业务数据，再写入 SMP-*）
seed-sample:
	$(PYTHON) scripts/seed_sample_data.py
