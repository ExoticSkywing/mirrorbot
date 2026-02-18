FROM anasty17/mltb:latest

WORKDIR /app
RUN chmod 777 /app

# 优先处理依赖（利用缓存）
COPY requirements.txt .
RUN python3 -m venv mltbenv && \
    mltbenv/bin/pip install --no-cache-dir -r requirements.txt

# 最后复制代码（避免缓存干扰）
COPY . .

RUN sed -i 's/\r$//' *.sh

# 添加验证步骤（查看文件内容）
RUN ls -l && \
    cat /app/bot/modules/services.py

CMD ["bash", "start.sh"]