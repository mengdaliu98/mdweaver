# mdweave on Railway.
#
# The image is the tool only. The documents are a separate, private repository
# cloned at boot by deploy/start.sh -- see DEPLOY.md.

FROM python:3.12-slim

# git is not a build dependency, it is a runtime one: the Checkpoint button
# shells out to it, and the content is cloned on startup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY mdweave ./mdweave
RUN pip install --no-cache-dir .

COPY deploy/start.sh /usr/local/bin/mdweave-start
RUN chmod +x /usr/local/bin/mdweave-start

# Where start.sh puts the clone. A Railway volume can be mounted here to keep
# edits that were never checkpointed; without one, git is the storage.
ENV MDWEAVE_CONTENTS=/data/knowledge_base
VOLUME ["/data"]

CMD ["mdweave-start"]
