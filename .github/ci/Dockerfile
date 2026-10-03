# What the suite assumes of its host: Python 3.14, git, a zsh login shell and an installed claude.
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git zsh curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/zsh ci
USER ci
ENV PATH=/home/ci/.local/bin:$PATH
RUN curl -fsSL https://claude.ai/install.sh | bash
WORKDIR /home/ci/src
