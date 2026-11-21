# Same base image the organisers labelled the commits in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The selector, its map, and the Python files of sqlglot at commit eb11aec
# (src.tgz), which it needs to read the code before each patch.
COPY selector/ /app/
RUN mkdir -p /opt/sqlglot \
    && tar xzf /app/src.tgz -C /opt/sqlglot \
    && rm /app/src.tgz \
    && python -I -S /app/select_tests.py --prepare \
    && python -m compileall -q /opt/sqlglot/sqlglot \
    && chmod -R a+rX /opt/sqlglot /app

CMD ["python", "-I", "-S", "-B", "/app/select_tests.py"]
