#!/usr/bin/env bash
# Generate a self-signed TLS cert for the booth (LAN use). Run on the Pi.
# Creates /opt/photobooth/certs/{cert.pem,key.pem}; the backend auto-enables HTTPS
# when those exist (see run-backend.sh). Re-run to refresh (e.g. after an IP change).
set -euo pipefail
APP="${APP:-/opt/photobooth}"
CERTDIR="$APP/certs"
DAYS="${DAYS:-825}"

mkdir -p "$CERTDIR"
IP="$(hostname -I | awk '{print $1}')"
HOST="$(hostname)"
# Extra IPs/hostnames as args (e.g. the guest-hotspot IP 192.168.50.1) so the cert is
# valid there too: ./gen-cert.sh 192.168.50.1
SAN="DNS:localhost,DNS:$HOST,IP:127.0.0.1,IP:$IP"
for extra in "$@"; do
  case "$extra" in
    *[a-zA-Z]*) SAN="$SAN,DNS:$extra" ;;   # looks like a hostname
    *)          SAN="$SAN,IP:$extra" ;;    # looks like an IP
  esac
done
echo "Generating self-signed cert for IP=$IP host=$HOST SAN=[$SAN] (valid ${DAYS} days)"

openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
  -keyout "$CERTDIR/key.pem" -out "$CERTDIR/cert.pem" \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=$SAN"

chmod 600 "$CERTDIR/key.pem"
echo "Done: $CERTDIR/cert.pem"
echo "Restart the app to enable HTTPS:  systemctl restart photobooth.service"
echo "NOTE: it's self-signed, so browsers show a one-time trust warning."
