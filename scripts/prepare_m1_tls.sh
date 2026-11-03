#!/usr/bin/env bash
# A private CA for control-plane -> local gateway TLS only. Browsers never see
# this certificate and must not bypass certificate interstitials or trust it.
set -euo pipefail
if [[ $# != 1 ]]; then echo 'Usage: prepare_m1_tls.sh PROJECT_ROOT' >&2; exit 2; fi
project_root=$(realpath "$1")
[[ -f "$project_root/infra/m1.compose.yml" ]]
tls_dir="$project_root/data/deployment"
install -d -m 0700 "$tls_dir"
if [[ -e "$tls_dir/ca.key" || -e "$tls_dir/gateway.key" || -e "$tls_dir/ca.pem" ]]; then
  echo 'TLS files already exist; refusing to rotate trust automatically.' >&2
  exit 2
fi
umask 077
cat > "$tls_dir/ca.cnf" <<'CA'
[req]
distinguished_name=dn
x509_extensions=ca_extensions
prompt=no
[dn]
CN=Video AI M1 Internal CA
[ca_extensions]
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
CA
openssl req -x509 -newkey rsa:3072 -nodes -days 90 \
  -keyout "$tls_dir/ca.key" -out "$tls_dir/ca.pem" \
  -config "$tls_dir/ca.cnf" 2>/dev/null
openssl req -newkey rsa:3072 -nodes -keyout "$tls_dir/gateway.key" -out "$tls_dir/gateway.csr" \
  -subj '/CN=video-ai-gateway.internal' 2>/dev/null
cat > "$tls_dir/gateway.ext" <<'EXT'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:video-ai-gateway.internal
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EXT
openssl x509 -req -in "$tls_dir/gateway.csr" -CA "$tls_dir/ca.pem" -CAkey "$tls_dir/ca.key" \
  -CAcreateserial -days 30 -out "$tls_dir/gateway.pem" -extfile "$tls_dir/gateway.ext" 2>/dev/null
chown 10001:10001 "$tls_dir/gateway.key"
chmod 0400 "$tls_dir/gateway.key"
chmod 0644 "$tls_dir/ca.pem" "$tls_dir/gateway.pem"
echo 'Internal gateway TLS created; private keys were not printed.'
