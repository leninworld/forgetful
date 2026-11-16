ARG PYTHON_BASE
FROM ${PYTHON_BASE}
WORKDIR /opt/app
ENV UV_PYTHON_DOWNLOADS=never UV_HTTP_RETRIES=0 UV_HTTP_TIMEOUT=30 UV_LINK_MODE=copy
RUN mkdir -p /opt/apt-evidence/bootstrap/inrelease && \
    (apt-get -o Debug::Acquire::gpgv=true update > /opt/apt-evidence/bootstrap/acquisition.log 2>&1 || { cat /opt/apt-evidence/bootstrap/acquisition.log; exit 1; }) && \
    (apt-get install -y --no-install-recommends ca-certificates curl gnupg git > /opt/apt-evidence/bootstrap/install.log 2>&1 || { cat /opt/apt-evidence/bootstrap/install.log; exit 1; }) && \
    dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' > /opt/apt-evidence/bootstrap/installed.tsv && \
    find /var/lib/apt/lists -type f ! -name lock -exec sha256sum {} + > /opt/apt-evidence/bootstrap/package-index-sha256.txt && \
    find /var/lib/apt/lists -type f -name '*InRelease' -exec cp {} /opt/apt-evidence/bootstrap/inrelease/ \; && \
    rm -rf /var/lib/apt/lists/*
COPY packet/pgdg-key.asc /tmp/pgdg-key.asc
RUN echo "0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76  /tmp/pgdg-key.asc" | sha256sum -c - && \
    test "$(gpg --show-keys --with-colons /tmp/pgdg-key.asc | awk -F: '$1 == "fpr" {print $10; exit}')" = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8" && \
    install -m 644 /tmp/pgdg-key.asc /usr/share/keyrings/forgetful-pgdg.asc && \
    echo 'deb [signed-by=/usr/share/keyrings/forgetful-pgdg.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' > /etc/apt/sources.list.d/forgetful-pgdg.list && \
    mkdir -p /opt/apt-evidence/pgdg/inrelease && \
    gpg --show-keys --with-colons /tmp/pgdg-key.asc > /opt/apt-evidence/pgdg/key-fingerprints.txt && \
    sha256sum /tmp/pgdg-key.asc > /opt/apt-evidence/pgdg/key-sha256.txt && \
    (apt-get -o Debug::Acquire::gpgv=true update > /opt/apt-evidence/pgdg/acquisition.log 2>&1 || { cat /opt/apt-evidence/pgdg/acquisition.log; exit 1; }) && \
    (apt-get install -y --no-install-recommends postgresql-client-16 > /opt/apt-evidence/pgdg/install.log 2>&1 || { cat /opt/apt-evidence/pgdg/install.log; exit 1; }) && \
    dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' > /opt/apt-evidence/pgdg/installed.tsv && \
    apt-cache policy postgresql-client-16 libpq5 > /opt/apt-evidence/pgdg/client-policy.txt && \
    find /var/lib/apt/lists -type f ! -name lock -exec sha256sum {} + > /opt/apt-evidence/pgdg/package-index-sha256.txt && \
    find /var/lib/apt/lists -type f -name '*InRelease' -exec cp {} /opt/apt-evidence/pgdg/inrelease/ \; && \
    rm -rf /var/lib/apt/lists/*
COPY inputs/uv-0.9.30-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl /tmp/uv.whl
COPY inputs/ruff-0.16.10-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl /tmp/ruff.whl
RUN python -c 'import zipfile,pathlib,os; [(lambda z,b: (pathlib.Path("/usr/local/bin/"+b).write_bytes(z.read(next(n for n in z.namelist() if n.endswith("/"+b)))),os.chmod("/usr/local/bin/"+b,0o755)))(zipfile.ZipFile("/tmp/"+b+".whl"),b) for b in ["uv","ruff"]]' && uv --version && ruff --version
COPY packet/build-constraints.txt /opt/build-constraints.txt
RUN uv venv --python /usr/local/bin/python /buildenv && uv pip install --python /buildenv/bin/python --no-deps -r /opt/build-constraints.txt && uv pip freeze --python /buildenv/bin/python > /opt/build-freeze.txt
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --all-extras --dev --no-install-project && uv pip freeze --python /opt/app/.venv/bin/python > /opt/app-freeze.txt
ENV PATH=/opt/app/.venv/bin:/usr/local/bin:/usr/bin:/bin
