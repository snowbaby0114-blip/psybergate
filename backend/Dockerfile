FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 --user-group app

WORKDIR /app

# The requirements come first so that this layer is rebuilt only when they change, and the test
# dependencies are installed too because the tests run inside this image. pip's warning about
# running as root protects a distribution-managed Python; this interpreter belongs to the image.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --root-user-action=ignore -r requirements.txt -r requirements-dev.txt

# Everything .dockerignore does not exclude, tests/ included (it also keeps env files out).
COPY . .

# CR is stripped in case the script was checked out with Windows line endings: the kernel would
# look for an interpreter called "/bin/sh\r" and the container would die with "no such file or
# directory". /app belongs to the app user because pytest and hypothesis write their caches
# next to the tests.
RUN sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh \
    && chown -R app:app /app

USER app

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["serve"]
