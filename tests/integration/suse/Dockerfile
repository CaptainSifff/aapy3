ARG BASE
FROM ${BASE}

RUN zypper --non-interactive refresh && \
    zypper --non-interactive install --no-recommends \
        python3 coreutils findutils gzip tar curl \
        make gcc rpm-build createrepo_c ncurses-devel
