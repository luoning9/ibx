# IBX Frontend

Vue 3 + TypeScript + Vite + Element Plus frontend scaffold.

## Start

```bash
conda activate ibx
cd /Users/jason/Documents/GitHub/ibx/frontend
npm install
npm run dev
```

默认会通过 Vite 代理访问后端：

- 前端请求前缀：`/api/v1`
- 代理目标：`http://127.0.0.1:8000`

可在 `frontend/.env` 覆盖（参考 `frontend/.env.example`）。

## Build

```bash
conda activate ibx
cd /Users/jason/Documents/GitHub/ibx/frontend
npm run build
```

## Current Routes

- `/strategies` 策略列表
- `/events` 运行事件
- `/positions` 持仓情况
- `/trade-instructions` 交易指令

## API Binding

页面已接入后端接口：

- `GET /v1/strategies`
- `POST /v1/strategies/{id}/cancel`
- `GET /v1/events`
- `GET /v1/portfolio-summary`
- `GET /v1/positions`
- `GET /v1/trade-instructions/active`
- `GET /v1/trade-logs`
