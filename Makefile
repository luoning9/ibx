.PHONY: check portfolio

CHECK_API_PORT := 4002
CHECK_PORTS ?= $(CHECK_API_PORT)

# 检查网关可用性（TCP + API 握手）
check:
	IB_PORTS=$(CHECK_PORTS) IB_API_PORT=$(CHECK_API_PORT) python3 scripts/check_ib_gateway.py

# 查看当前资产组合
portfolio:
	IB_API_PORT=$(CHECK_API_PORT) python3 scripts/list_portfolio.py
