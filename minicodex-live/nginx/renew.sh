#!/bin/sh
# The six-day certificate loop.
#
# Let's Encrypt's IP address certificates exist only in the `shortlived`
# profile -- 160 hours, and the official guidance is to renew every two to
# three days.  So this container does not run cron-once-a-day like a classic
# certbot deployment; it runs `certbot renew` on a loop short enough that a
# missed cycle never matters: certbot itself renews when inside 30 days of
# expiry, which for a six-day cert is *immediately*, every run.
#
# Two phases, one entrypoint:
#
#   1. Issue (first boot, or line 1 of the server IP changed): `certbot
#      certonly --ip-address ... --webroot`.  Skipped when a live cert for
#      this IP already exists.
#   2. Renew forever: `certbot renew` every 12h -- roughly 1/12 of the
#      cert's life, so no clock drift or outage eats the margin.  Each
#      successful renewal fires the deploy hook in compose.yaml, which
#      reloads nginx.
#
# Flags worth their comment, from Let's Encrypt's Certbot announcement
# (2026-03-11) and docs:
#   --ip-address            new in Certbot 5.3; requires 5.4+ for webroot
#   --preferred-profile     shortlived is *mandatory* for IP certs (LE rule,
#                           not a choice: IP identifiers are only issued in
#                           the six-day profile)
#   --staging               THIS SCRIPT DEFAULTS TO STAGING.  Let's Encrypt
#                           rate-limits production issuance, and a broken
#                           server that retries every 12h can burn the
#                           whole IP's quota in a day.  Set ACME_STAGING=0
#                           once a browser accepts the challenge path, then
#                           delete ./nginx/certs-stamp to force reissue.
set -eu

SERVER_IP="${SERVER_IP:?set SERVER_IP to this machine's public IPv4/IPv6 in compose.yaml}"
ACME_STAGING="${ACME_STAGING:-1}"

CERT_DIR="/etc/letsencrypt/live/${SERVER_IP}"
STAMP_DIR="/letsencrypt-stamp"

STAGING_FLAG=""
if [ "$ACME_STAGING" = "1" ]; then
    STAGING_FLAG="--staging"
    echo "certbot: running against the STAGING endpoint (ACME_STAGING=1)"
else
    echo "certbot: running against PRODUCTION"
fi

issue() {
    certbot certonly \
        --non-interactive \
        --agree-tos \
        --register-unsafely-without-email \
        $STAGING_FLAG \
        --ip-address "$SERVER_IP" \
        --webroot --webroot-path /var/www/certbot \
        --preferred-profile shortlived \
        --deploy-hook "touch ${STAMP_DIR}/reload"
    mkdir -p "$STAMP_DIR"
    touch "${STAMP_DIR}/reload"   # nginx needs a first load too
}

# A live directory is per-IP: a changed IP starts a new identity, not a
# renewal of the old one.
if [ ! -d "$CERT_DIR" ]; then
    issue
else
    echo "certbot: cert for ${SERVER_IP} exists; entering renew loop"
fi

while :; do
    if certbot renew \
        --non-interactive \
        $STAGING_FLAG \
        --webroot --webroot-path /var/www/certbot \
        --deploy-hook "touch ${STAMP_DIR}/reload"; then
        : # renewed (or not due); the hook marks nginx's work for it
    else
        echo "certbot: renew failed this cycle; retrying next interval" >&2
    fi
    sleep "${RENEW_INTERVAL_SECONDS:-43200}"
done
