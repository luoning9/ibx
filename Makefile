.PHONY: up down logs shell check portfolio

TRADING_MODE ?= $(shell awk -F= '/^TRADING_MODE=/{print $$2}' .env 2>/dev/null | tr -d '[:space:]')
MODE := $(if $(TRADING_MODE),$(TRADING_MODE),paper)
CHECK_API_PORT ?= $(if $(filter live,$(MODE)),4001,4002)
CHECK_PORTS ?= $(CHECK_API_PORT)

# 启动网关
up:
	docker-compose up -d

# 停止网关
down:
	docker-compose down

# 查看实时日志
logs:
	docker-compose logs -f

# 进入容器终端
shell:
	docker exec -it ibx-gateway /bin/bash

# 检查网关可用性（TCP + API 握手）
check:
	IB_PORTS=$(CHECK_PORTS) IB_API_PORT=$(CHECK_API_PORT) python3 scripts/check_ib_gateway.py

# 查看当前资产组合
portfolio:
	IB_API_PORT=$(CHECK_API_PORT) python3 scripts/list_portfolio.py
