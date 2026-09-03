FROM python:3.12-slim

WORKDIR /app

# 系统依赖（使用阿里云源加速国内构建）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY qingtian/requirements.txt .
# 使用清华 PyPI 镜像加速
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY qingtian/ ./qingtian/

# 修复 entrypoint 权限
RUN chmod +x /app/qingtian/scripts/entrypoint.sh

WORKDIR /app/qingtian

EXPOSE 1996

# 入口点：先初始化配置（首次启动生成 config.yaml），再启动服务
ENTRYPOINT ["/app/qingtian/scripts/entrypoint.sh"]
CMD []
