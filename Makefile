.PHONY: up down logs shell

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