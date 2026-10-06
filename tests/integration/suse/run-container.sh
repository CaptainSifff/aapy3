#!/bin/sh
set -eu

: "${MATRIX_OS:?MATRIX_OS must identify the SUSE image}"

mkdir -p /work/repo /results
cp -a /source/. /work/repo/
mkdir -p /work/repo/ports
cp -a /work/repo/tests/fixtures/ports/. /work/repo/ports/

recipe=/work/repo/ports/editors/nano
mkdir -p "$recipe/distfiles"
cp /input/nano-7.1.tar.gz "$recipe/distfiles/nano-7.1.tar.gz"
printf 'cc9e42c4805193f9dc3ae22b9644bb1d  %s\n' \
    "$recipe/distfiles/nano-7.1.tar.gz" | md5sum --check

mkdir -p /usr/local/nacharbeiten/na_bin /usr/local/bin
install -m 0755 /work/repo/tests/integration/suse/getdistro \
    /usr/local/nacharbeiten/na_bin/getdistro
cat > /usr/local/bin/aap <<'EOF'
#!/bin/sh
exec python3 /work/repo/tests/adapters/aap_cli.py "$@"
EOF
chmod 0755 /usr/local/bin/aap

export AAP_REPOSITORY=/work/repo
export AAP=/usr/local/bin/aap
export AAP_RECURSIVE_TRACE=/results/${MATRIX_OS}-events.jsonl
export AAP_RECURSIVE_INVOCATION=primary
: > "$AAP_RECURSIVE_TRACE"

cd "$recipe"
if ! "$AAP" rpm > "/results/${MATRIX_OS}-build.log" 2>&1; then
    cat "/results/${MATRIX_OS}-build.log" >&2
    exit 1
fi

rpm_file=$(find /export/company -type f \
    -name 'company.nano-7.1-1.noarch.rpm' -print -quit)
if [ -z "$rpm_file" ]; then
    echo 'A-A-P completed without producing company.nano RPM' >&2
    exit 1
fi
cp "$rpm_file" "/results/${MATRIX_OS}-nano.rpm"

identity=$(rpm -qp --queryformat '%{NAME} %{VERSION} %{RELEASE} %{ARCH}' "$rpm_file")
test "$identity" = 'company.nano 7.1 1 noarch'
rpm -qpl "$rpm_file" | grep -Fx /usr/local/bin/nano

rpm -i "$rpm_file"
rpm -q company.nano
/usr/local/bin/nano --version | grep -F 'GNU nano, version 7.1'
/usr/local/bin/nano --help > /dev/null
rpm -V company.nano

cat > "/results/${MATRIX_OS}-summary.txt" <<EOF
target=$MATRIX_OS
rpm=$identity
rpm_path=$rpm_file
smoke=rpm-query,rpm-file-list,rpm-install,nano-version,nano-help,rpm-verify
EOF
