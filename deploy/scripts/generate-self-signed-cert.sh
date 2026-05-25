#!/usr/bin/env bash
set -euo pipefail

SERVER_NAME="${1:-localhost}"
SERVER_IP="${2:-127.0.0.1}"
CERT_DIR="${3:-/etc/ssl/work-on-holiday}"
DAYS="${DAYS:-365}"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"

CONFIG_FILE="$CERT_DIR/openssl-self-signed.cnf"
cat > "$CONFIG_FILE" <<CONFIG
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = $SERVER_NAME

[v3_req]
subjectAltName = @alt_names
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
DNS.1 = $SERVER_NAME
IP.1 = $SERVER_IP
CONFIG

openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
  -keyout "$CERT_DIR/server.key" \
  -out "$CERT_DIR/server.crt" \
  -config "$CONFIG_FILE"

chmod 600 "$CERT_DIR/server.key"
chmod 644 "$CERT_DIR/server.crt"

openssl x509 -in "$CERT_DIR/server.crt" -noout -subject -dates -ext subjectAltName
