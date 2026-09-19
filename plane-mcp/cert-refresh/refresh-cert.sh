#!/usr/bin/env bash
# Install the Plane MCP's renewed Roles Anywhere certificate, if there is one
# (HOME-389).
#
# The home cert portal renews plane-mcp automatically 30 days before expiry and
# publishes the new pair to SSM. This fetches it using the CURRENT certificate
# (the plane-mcp profile), so no admin credentials are involved — and once the
# current certificate has expired, there is nothing left to fetch with, which
# is deliberate: a lapsed identity needs a human.
#
# Safe to run as often as you like: it does nothing unless the published
# certificate differs from the installed one, and it only swaps once the new
# pair has proved it can assume the role.
set -euo pipefail

# launchd's PATH is minimal.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROFILE="${PLANE_MCP_AWS_PROFILE:-plane-mcp}"
REGION="eu-west-2"
DEPLOY_ENV="${DEPLOY_ENV:-prod}"
CN="plane-mcp"
DEST="$HOME/.config/plane-mcp"
CERT="$DEST/plane-mcp.crt.pem"
KEY="$DEST/plane-mcp.key.pem"
CERT_PARAM="/plane-mcp/${DEPLOY_ENV}/client-cert"
KEY_PARAM="/plane-mcp/${DEPLOY_ENV}/client-key"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
die() { log "error: $*"; exit 1; }

[ -f "$CERT" ] || die "no installed certificate at $CERT — run home-servers' collect-cert.sh --role plane-mcp first"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

fingerprint() { openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2; }

# ── Is there anything new? ────────────────────────────────────────────────────
if ! aws ssm get-parameter --profile "$PROFILE" --region "$REGION" --name "$CERT_PARAM" \
        --query Parameter.Value --output text > "$WORK/cert.pem" 2> "$WORK/err"; then
    if grep -q ParameterNotFound "$WORK/err"; then
        log "nothing published yet at ${CERT_PARAM}; the first renewal will create it"
        exit 0
    fi
    cat "$WORK/err" >&2
    if grep -q AccessDenied "$WORK/err"; then
        die "the plane-mcp-sync role may not read ${CERT_PARAM} — is nakom.is's LambdaStack (HOME-389) deployed?"
    fi
    if ! openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null; then
        die "the installed certificate has expired, so it can't fetch its successor — re-issue it (collect-cert.sh --role plane-mcp)"
    fi
    die "could not read ${CERT_PARAM}"
fi

if [ "$(fingerprint "$WORK/cert.pem")" = "$(fingerprint "$CERT")" ]; then
    log "up to date (expires $(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2))"
    exit 0
fi

# ── Fetch and check the new pair ─────────────────────────────────────────────
aws ssm get-parameter --profile "$PROFILE" --region "$REGION" --name "$KEY_PARAM" --with-decryption \
    --query Parameter.Value --output text > "$WORK/key.pem" || die "could not read ${KEY_PARAM}"
chmod 600 "$WORK/key.pem"

NEW_CN=$(openssl x509 -in "$WORK/cert.pem" -noout -subject | sed -n 's/.*CN *= *\([^,/]*\).*/\1/p' | sed 's/[[:space:]]*$//')
[ "$NEW_CN" = "$CN" ] || die "published certificate has CN '${NEW_CN}', expected '${CN}' — not installing"
openssl x509 -in "$WORK/cert.pem" -noout -checkend 0 >/dev/null || die "published certificate has already expired — not installing"

# The key and certificate were written separately; make sure they belong together.
[ "$(openssl x509 -in "$WORK/cert.pem" -noout -pubkey)" = "$(openssl pkey -in "$WORK/key.pem" -pubout)" ] \
    || die "published key does not match published certificate — not installing"

# An older certificate than the one installed would be a step backwards.
if [ "$(openssl x509 -in "$WORK/cert.pem" -noout -enddate | cut -d= -f2 | xargs -I{} date -j -f '%b %e %T %Y %Z' '{}' +%s)" \
     -le "$(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2 | xargs -I{} date -j -f '%b %e %T %Y %Z' '{}' +%s)" ]; then
    die "published certificate expires no later than the installed one — not installing"
fi

# ── Prove it before relying on it ─────────────────────────────────────────────
# Reuse the trust anchor, profile and role ARNs from the existing
# credential_process line, which collect-cert.sh wrote and verified.
CRED_LINE=$(awk -v p="[profile $PROFILE]" '$0==p{f=1;next} /^\[/{f=0} f && /^credential_process/' "$HOME/.aws/config")
[ -n "$CRED_LINE" ] || die "no credential_process for [profile $PROFILE] in ~/.aws/config"
arg() { echo "$CRED_LINE" | sed -n "s/.*--$1 \([^ ]*\).*/\1/p"; }
HELPER=$(echo "$CRED_LINE" | sed -n 's/^credential_process *= *\([^ ]*\).*/\1/p')

"$HELPER" credential-process --certificate "$WORK/cert.pem" --private-key "$WORK/key.pem" \
    --trust-anchor-arn "$(arg trust-anchor-arn)" --profile-arn "$(arg profile-arn)" --role-arn "$(arg role-arn)" \
    > "$WORK/creds.json" 2> "$WORK/err" || { cat "$WORK/err" >&2; die "new certificate could not assume the role — not installing"; }
grep -q AccessKeyId "$WORK/creds.json" || die "helper returned no credentials — not installing"

# ── Swap ─────────────────────────────────────────────────────────────────────
# Same paths as before, so ~/.aws/config needs no change, and the Plane MCP
# picks the new pair up the next time its hour-long credentials refresh.
mkdir -p "$DEST/previous"
cp -p "$CERT" "$DEST/previous/plane-mcp.crt.pem"
cp -p "$KEY" "$DEST/previous/plane-mcp.key.pem"
install -m 0600 "$WORK/key.pem" "$KEY.new" && mv -f "$KEY.new" "$KEY"
install -m 0644 "$WORK/cert.pem" "$CERT.new" && mv -f "$CERT.new" "$CERT"

log "installed renewed certificate, expires $(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2); previous pair kept in ${DEST}/previous/"
