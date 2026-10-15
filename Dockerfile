FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONTACT_SUBMISSIONS_DB=/data/contact-submissions.sqlite \
    PORT=3000

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /data \
    && chown 10001:10001 /app /data

WORKDIR /app
COPY --chown=10001:10001 app.py server.py ./

USER 10001:10001
EXPOSE 3000
CMD ["python", "server.py"]
