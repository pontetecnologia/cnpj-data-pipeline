#!/usr/bin/env bash
# Execução agendada (cron) da prospecção Receita -> GestaoRevenda.
# Sobe o Postgres do pipeline (docker compose) e roda rodar_mensal.py com o Python do .venv.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d postgres >/dev/null
for _ in $(seq 1 30); do
  docker exec cnpj-pipeline-postgres pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 2
done

exec .venv/bin/python rodar_mensal.py "$@"
