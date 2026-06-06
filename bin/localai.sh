#!/usr/bin/env bash
#
# Controls the localai instance. Thin wrapper around service.sh.
exec "$( dirname -- "${BASH_SOURCE[0]}" )/service.sh" localai "$@"
