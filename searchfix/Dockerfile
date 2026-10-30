FROM python:3.12-alpine
WORKDIR /app
COPY rank.py server.py library.py fuzzy.py searchlog.py expand.py toplist.py ./
RUN adduser -D -H -u 10001 searchfix && mkdir /data && chown searchfix /data
USER searchfix
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
CMD ["python", "/app/server.py"]
