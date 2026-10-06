ARG BASE
FROM ${BASE}

RUN zypper --non-interactive refresh && \
    zypper --non-interactive install --no-recommends \
        python3 coreutils findutils gzip tar curl \
        make gcc rpm-build createrepo_c ncurses-devel

COPY tests/integration/suse/run-container.sh /usr/local/bin/run-aap-package-test
RUN chmod 0755 /usr/local/bin/run-aap-package-test

ENTRYPOINT ["/usr/local/bin/run-aap-package-test"]
