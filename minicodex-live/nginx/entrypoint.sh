#!/bin/sh
# nginx's side of the certificate handoff.
#
# The certbot container cannot signal nginx directly (no docker socket, on
# purpose -- giving a renewal container root-equivalent over the whole host
# to shave one file off a mount is the wrong trade).  Instead both sides
# share a tiny stamp volume: certbot touches `reload` after every renewal,
# this loop notices, asks nginx to re-read the pem files, and clears it.
#
# A reload (not a restart) keeps existing connections -- including the
# WebSocket event streams -- alive across the swap.  Missing cert at boot:
# this exits nonzero and compose restarts the container, which is the
# documented failure mode ("a TLS proxy without TLS is a redirect to
# nowhere") rather than a silent 80-only server.
set -eu

SERVER_IP="${SERVER_IP:?set SERVER_IP to this machine's public IPv4/IPv6 in compose.yaml}"
CERT="/etc/nginx/certs/live/${SERVER_IP}/fullchain.pem"

if [ ! -f "$CERT" ]; then
    echo "nginx: no certificate for ${SERVER_IP} yet -- waiting for certbot" >&2
    exit 1
fi

# Render the template with this deployment's IP.  envsubst ships in the
# nginx image; only SERVER_IP is substituted, so nginx's own $host /
# $http_upgrade variables in the body pass through untouched.
envsubst '$SERVER_IP' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

nginx -t || exit 1
nginx -g "daemon off;" &
NGINX_PID=$!

while :; do
    sleep "${RELOAD_POLL_SECONDS:-5}"
    if [ -f /letsencrypt-stamp/reload ]; then
        rm -f /letsencrypt-stamp/reload
        echo "nginx: new certificate detected, reloading"
        nginx -t && nginx -s reload
    fi
done
