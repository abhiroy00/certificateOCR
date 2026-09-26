#!/usr/bin/env bash
# Run ONCE on a fresh Ubuntu EC2 instance (22.04/24.04) to prepare it to run
# this project's existing headless CLI (share_ocr/cli.py) - no GUI, no code
# changes, same pipeline/extractor/config logic that runs on Windows today.
#
#   scp -r . ubuntu@<ec2-ip>:~/certificateOCR
#   ssh ubuntu@<ec2-ip>
#   cd certificateOCR && bash deploy/aws/setup_ec2.sh
#
set -euo pipefail

echo "== apt packages =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip poppler-utils tesseract-ocr tmux curl unzip

echo "== AWS CLI v2 =="
if ! command -v aws >/dev/null 2>&1; then
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
    unzip -q -o /tmp/awscliv2.zip -d /tmp
    sudo /tmp/aws/install
fi

echo "== python venv =="
cd "$(dirname "$0")/../.."           # repo root
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "== workdir =="
# Keeps the queue.db / CSV shards off the small root volume - attach and
# mount a data EBS volume at /data before running this, or just leave the
# default (~/.share_ocr) if root volume storage is enough for your run.
mkdir -p "${SHARE_OCR_HOME:-$HOME/.share_ocr}"

echo "== systemd unit =="
sudo install -m 0644 deploy/aws/share-ocr.service /etc/systemd/system/share-ocr.service
touch "$HOME/.share_ocr_web.env"
if ! grep -q '^SHARE_OCR_WEB_SECRET=' "$HOME/.share_ocr_web.env"; then
    printf 'SHARE_OCR_WEB_SECRET=%s\n' "$(openssl rand -hex 32)" >> "$HOME/.share_ocr_web.env"
fi
chmod 600 "$HOME/.share_ocr_web.env"
sudo systemctl daemon-reload

cat <<'EOF'

Setup done. Next:

1. Put API keys in the environment (NEVER in a file that gets committed):
     export OPENAI_API_KEYS="sk-...,sk-...,sk-..."
     export NVIDIA_API_KEYS="nvapi-...,nvapi-..."
   (or use AWS Systems Manager Parameter Store - see deploy/aws/README.md
   "Where the API keys live on the server")

2. Get your scans onto this box (pick one):
     aws s3 sync s3://your-bucket/scans ~/scans
     # or scp -r ./scans ubuntu@<ec2-ip>:~/scans

3. Run it (same CLI that already ships with this project):
     source .venv/bin/activate
     python -m share_ocr.cli run ~/scans --workers 8 --engine openai

   Run inside tmux so it keeps going after you disconnect:
     tmux new -s ocr
     python -m share_ocr.cli run ~/scans --workers 8 --engine openai
     # Ctrl+B then D to detach; `tmux attach -t ocr` to check back in

   The browser UI is started as a persistent service after configuring SSM
   and the security-group port rule; see deploy/aws/README.md.

4. Get the results back:
     aws s3 sync ~/.share_ocr/csv s3://your-bucket/results
     # or scp -r ubuntu@<ec2-ip>:~/.share_ocr/csv ./results

Browser UI is installed. It uses the same email + admin-relayed OTP login as
the desktop app. Configure the SMTP sender parameters and EC2 role in SSM,
then start it with `sudo systemctl enable --now share-ocr` (see README).

EOF
