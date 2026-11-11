FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d

RUN printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261111T000000Z bookworm main\n' > /etc/apt/sources.list \
    && rm /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libosmesa6 libgl1 \
    && rm -rf /var/lib/apt/lists/*
ENV MUJOCO_GL=osmesa PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
COPY requirements.lock /opt/runtime/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/runtime/requirements.lock
COPY openroboto_task_runtime-0.3.19-py3-none-any.whl /opt/runtime/
RUN pip install --no-cache-dir /opt/runtime/openroboto_task_runtime-0.3.19-py3-none-any.whl \
    && python -m pip check
COPY task_sources.json /usr/local/lib/python3.11/site-packages/configs/task_sources.json
ARG RUNTIME_CONTENT_SHA256
LABEL org.openroboto.runtime.version="0.3.19" org.openroboto.runtime.content-sha256=$RUNTIME_CONTENT_SHA256
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "task_source.miner"]
