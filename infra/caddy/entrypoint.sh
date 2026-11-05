#!/bin/sh
set -eu

# Support existing .env files as well as explicit overrides. Caddy needs a bare
# host for default_sni, while the site address can contain a scheme and port.
if [ -z "${XAGENT_DEFAULT_SNI:-}" ]; then
    site="${XAGENT_SITE_ADDRESS:-localhost}"
    site="${site#http://}"
    site="${site#https://}"
    site="${site%%/*}"
    case "$site" in
        \[*\]*) site="${site#\[}"; site="${site%%\]*}" ;;
        *) site="${site%%:*}" ;;
    esac
    export XAGENT_DEFAULT_SNI="${site:-localhost}"
fi

exec "$@"
