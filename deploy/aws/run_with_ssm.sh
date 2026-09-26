#!/usr/bin/env bash
# Pulls API keys from AWS Systems Manager Parameter Store (SecureString) at
# start time instead of keeping them in a systemd unit file or a .env file on
# disk - the recommended way to run this. Requires the EC2 instance's IAM
# role to have ssm:GetParameter on these two parameter names (see README.md
# "IAM policy for the EC2 instance role").
#
#   aws ssm put-parameter --name /share-ocr/openai-keys --type SecureString \
#       --value "sk-...,sk-..."
#   aws ssm put-parameter --name /share-ocr/nvidia-keys --type SecureString \
#       --value "nvapi-...,nvapi-..."
#
# Usage: same arguments as the CLI itself, e.g.
#   bash deploy/aws/run_with_ssm.sh run ~/scans --workers 24 --engine openai
set -euo pipefail
cd "$(dirname "$0")/../.."
source .venv/bin/activate

export OPENAI_API_KEYS="$(aws ssm get-parameter --name /share-ocr/openai-keys \
    --with-decryption --query Parameter.Value --output text 2>/dev/null || true)"
export NVIDIA_API_KEYS="$(aws ssm get-parameter --name /share-ocr/nvidia-keys \
    --with-decryption --query Parameter.Value --output text 2>/dev/null || true)"
export SHARE_OCR_SMTP_USER="$(aws ssm get-parameter --name /share-ocr/smtp-user \
    --with-decryption --query Parameter.Value --output text 2>/dev/null || true)"
export SHARE_OCR_SMTP_PASSWORD="$(aws ssm get-parameter --name /share-ocr/smtp-password \
    --with-decryption --query Parameter.Value --output text 2>/dev/null || true)"

if [ -z "$OPENAI_API_KEYS" ] && [ -z "$NVIDIA_API_KEYS" ]; then
    echo "No keys found in SSM (/share-ocr/openai-keys, /share-ocr/nvidia-keys)." >&2
    echo "Either put them there (see the comment at the top of this script)," >&2
    echo "or export OPENAI_API_KEYS / NVIDIA_API_KEYS yourself before running." >&2
    exit 1
fi

if [ "${1:-}" = "web" ]; then
    shift
    exec python -m share_ocr.web "$@"
fi

exec python -m share_ocr.cli "$@"
