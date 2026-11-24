FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

ENV DB_PATH=/data/oral_history.db
# 256MiB 级小地址空间环境下，glibc 默认每 CPU 一个 malloc arena 会预留过多
# 虚拟地址；限定 2 个 arena 可把启动 VmSize 从约 234MiB 降到约 170MiB。
ENV MALLOC_ARENA_MAX=2
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
