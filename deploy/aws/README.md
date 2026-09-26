# Running this project on AWS EC2

## Browser screen and OTP login

The browser UI now follows the desktop access flow: a user enters an email,
the OTP goes to the configured administrator inbox, and the administrator
relays the code. After verification, users can upload scans, run OCR, watch
progress, and download CSV output. The server needs the SMTP sender account
used by the desktop app. Keep its Gmail App Password in SSM, never GitHub.
`setup_ec2.sh` creates a random session secret in
`/home/ubuntu/.share_ocr_web.env` (mode 0600).

For public access, add **Custom TCP / 8000 / Anywhere-IPv4**. For real
certificate documents, put the site behind HTTPS before sharing widely.
The EC2 instance role must be able to read the API and SMTP parameters.
Start the service:

```bash
sudo systemctl enable --now share-ocr
sudo systemctl status share-ocr
```

Visit `http://<elastic-ip>:8000`. Enter an email and use the code relayed by
the administrator at `ADMIN_EMAIL`. The direct instance URL uses HTTP; for
certificate documents or broad sharing, put the UI behind HTTPS.
Uploaded scans keep their original file names. In the downloaded CSV, the
file name links to `http://<server>:8000/scan/...` with a signature, so
clicking it in Excel on your PC opens the scan. The link uses the address you
browsed to; to force a different one (e.g. an HTTPS domain), add
`SHARE_OCR_PUBLIC_URL=https://your-domain` to `~/.share_ocr_web.env`.
After changing web code or requirements, pull the update and rerun
`bash deploy/aws/setup_ec2.sh`, then restart the service.

## Use the already-created Mumbai instance

Instance: `i-03a4e9e2bf5b6324a` (`13.235.100.134`), region `ap-south-1`,
Ubuntu login user `ubuntu`. The deployment below needs the matching SSH
private key (`.pem`) or a working EC2 Instance Connect/SSM shell. Never send
the private key or API keys in chat.

From PowerShell at the repository root, first copy the application and its
AWS setup files (this avoids copying `.git`, local credentials and build
artifacts):

```powershell
$key = "$env:USERPROFILE\Downloads\share-ocr.pem" # set this to your .pem path
ssh -i $key ubuntu@13.235.100.134 "mkdir -p ~/certificateOCR"
scp -i $key -r share_ocr deploy requirements.txt ubuntu@13.235.100.134:~/certificateOCR/
ssh -i $key ubuntu@13.235.100.134 "cd ~/certificateOCR && bash deploy/aws/setup_ec2.sh"
```

The instance needs outbound internet access for Ubuntu packages and the OCR
API. Keep inbound access closed except SSH from your own IP (or use SSM).
The instance type has 8 GB RAM; the browser UI starts with 8 workers by
default. You choose the documents from your computer in the browser.

For unattended operation, attach an EC2 instance profile that can read the
two SecureString parameters below, then create those parameters using your
local AWS CLI profile. The profile used locally needs `ssm:PutParameter`;
the EC2 role only needs `ssm:GetParameter` on `/share-ocr/*` (and KMS decrypt
if using a customer-managed key). The instance role is separate from your
local deploy credentials.

```powershell
aws ssm put-parameter --region ap-south-1 --name /share-ocr/openai-keys `
  --type SecureString --overwrite --value "YOUR_OPENAI_KEY"
aws ssm put-parameter --region ap-south-1 --name /share-ocr/nvidia-keys `
  --type SecureString --overwrite --value "YOUR_NVIDIA_KEY"
aws ssm put-parameter --region ap-south-1 --name /share-ocr/smtp-user `
  --type SecureString --overwrite --value "YOUR_SENDER_GMAIL"
aws ssm put-parameter --region ap-south-1 --name /share-ocr/smtp-password `
  --type SecureString --overwrite --value "YOUR_GMAIL_APP_PASSWORD"
```

Only create a parameter for an engine you actually use. After uploading scans
and confirming the keys/instance role are in place, start and inspect the
service:

```bash
sudo systemctl enable --now share-ocr
sudo systemctl status share-ocr
journalctl -u share-ocr -f
```

To run without a service, use `tmux` and `bash deploy/aws/run_with_ssm.sh run
~/scans --workers 8 --engine openai` instead.

Nothing about the OCR pipeline changes here - `share_ocr/config.py`,
`extractor.py` (the OpenAI+NVIDIA key pool), `pipeline.py`, `db.py`,
`csv_writer.py` are exactly what already runs on Windows. EC2 just gives that
same code a machine to run on 24/7 without needing someone's laptop kept on.
The GUI (`share_ocr/gui.py`, the .exe) is a Tkinter desktop app and does not
belong on a headless server - use the CLI that already ships with this
project instead (`share_ocr/cli.py`, documented in the main README under
"Run headless for the big job"). Same queue, same CSV output, same Review
column, same duplicate detection - just no window.

## Do you still need an OpenAI/NVIDIA API key on EC2? Yes.

EC2 is only where the code executes. The actual certificate-reading happens
on OpenAI's/NVIDIA's servers over the internet - that call, and the key it
needs, is identical whether the request leaves from your laptop or from an
EC2 instance. Moving to EC2 does not remove that dependency and does not by
itself make individual API calls faster (that is bounded by OpenAI's/
NVIDIA's own response time) - what it buys you is a machine that stays on,
has stable networking, and can run unattended for the hours a big batch
takes. Speed still comes from the same lever as today: how many keys are in
the pool (extractor.KeyPool - more keys, more concurrent workers, see
`suggested_workers`), spread across different OpenAI organisations if you
want more than one org's rate limit.

If you would rather not depend on an external LLM API at all, the
alternative is re-pointing the vision calls at AWS's own OCR/vision service
(Textract, or Bedrock's Claude/Nova models) - a real re-architecture of
`extractor.py`, not a deployment change, and NOT what this pass does (kept
"structure exactly the same" as asked). Say the word if you want that
instead.

## 1. Get proper AWS credentials - not the root password

The screenshot shared in chat was a root account email + password. That
cannot be used for any of this (EC2/SSM/S3 automation needs an *access key*,
which a console password is not) and using root day-to-day is against AWS's
own guidance. Before anything else:

1. Log in to the AWS Console as root, go to **IAM -> Users -> Create user**.
2. Give it programmatic access with a scoped policy (EC2 + the S3 bucket
   below + SSM Parameter Store) - see the policy JSON below.
3. Generate an **access key** for that user (Access key ID + Secret access
   key) - this is what goes into `aws configure`, never the root password.
4. Turn on MFA on the root account and stop using it day-to-day.
5. **Change the root password now** - it was pasted into this chat.

IAM policy to attach to that deploy user (adjust the bucket name):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["ec2:RunInstances", "ec2:TerminateInstances",
      "ec2:DescribeInstances", "ec2:CreateSecurityGroup", "ec2:AuthorizeSecurityGroupIngress",
      "ec2:CreateTags", "ec2:CreateKeyPair", "ec2:DescribeKeyPairs",
      "ec2:DescribeImages", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets",
      "ec2:DescribeVpcs"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::YOUR-BUCKET", "arn:aws:s3:::YOUR-BUCKET/*"]},
    {"Effect": "Allow", "Action": ["ssm:PutParameter", "ssm:GetParameter"],
      "Resource": "arn:aws:ssm:*:*:parameter/share-ocr/*"}
  ]
}
```

Then locally: `aws configure --profile share-ocr-deploy` and paste that
user's access key/secret when prompted (never paste them into chat).

## 2. Pick the instance

The pipeline is I/O-bound (waiting on API responses), not CPU-bound, so you
do not need a large instance - RAM for the worker thread count and a decent
network path matter more than CPU cores.

| Batch size | Instance | vCPU / RAM | Approx on-demand cost |
|---|---|---|---|
| Up to ~5,000/day, testing | `t3.medium` | 2 / 4 GB | ~$0.04/hr |
| A few thousand-10k/day | `t3.large` | 2 / 8 GB | ~$0.08/hr |
| The 80,000-doc job, many keys/workers | `t3.xlarge` or `m6i.large` | 4 / 16 GB | ~$0.15-0.10/hr |

Ubuntu 22.04/24.04 LTS AMI. A small (30-50 GB) gp3 EBS root volume is plenty
for the queue.db + CSV shards; the scanned images themselves are usually the
biggest thing on disk, so size storage to however many GB your 80,000
documents actually are.

Security group: only open **port 22 (SSH)**, source = your own IP
(`aws ec2 authorize-security-group-ingress --cidr <your-ip>/32 ...`) - this
app has no reason to accept any other inbound traffic.

## 3. Launch it

```bash
export AWS_PROFILE=share-ocr-deploy
aws ec2 create-key-pair --key-name share-ocr --query 'KeyMaterial' --output text > share-ocr.pem
chmod 400 share-ocr.pem

aws ec2 run-instances \
  --image-id <ubuntu-24.04-ami-id-for-your-region> \
  --instance-type t3.xlarge \
  --key-name share-ocr \
  --security-group-ids <sg-id> \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=share-ocr}]'
```

## 4. Ship the code and set it up

```bash
scp -i share-ocr.pem -r . ubuntu@<ec2-public-ip>:~/certificateOCR
ssh -i share-ocr.pem ubuntu@<ec2-public-ip>
cd certificateOCR
bash deploy/aws/setup_ec2.sh
```

That installs Python, poppler (PDF support), creates a venv and
`pip install -r requirements.txt` - the exact same dependencies the Windows
build uses.

## 5. Where the API keys live on the server

Do not bake them into the AMI or commit them. Two options, both already
wired up:

* **Quick/manual:** `export OPENAI_API_KEYS="sk-...,sk-..."` and/or
  `export NVIDIA_API_KEYS="nvapi-...,nvapi-..."` in the shell before running
  the CLI (`share_ocr/secrets.py` reads these exact env vars, same as the
  desktop app's env-var precedence).
* **Recommended for anything long-running:** store them in SSM Parameter
  Store as `SecureString` (see the commands at the top of
  `run_with_ssm.sh`) and launch via that script instead of calling
  `share_ocr.cli` directly - it fetches and exports them at start time.
  Needs the instance's IAM role (not just your deploy user) to have
  `ssm:GetParameter` on `/share-ocr/*` - attach that as an instance profile
  at launch, or via `aws iam` afterwards.

## 6. Getting 80,000 scans onto the box, and results back off it

```bash
# once: create a bucket
aws s3 mb s3://your-share-ocr-bucket

# upload scans from wherever they are today
aws s3 sync ./scans s3://your-share-ocr-bucket/scans

# on the EC2 box
aws s3 sync s3://your-share-ocr-bucket/scans ~/scans

# run (foreground, in tmux so it survives disconnects)
tmux new -s ocr
source .venv/bin/activate
python -m share_ocr.cli run ~/scans --workers 24 --engine openai
# Ctrl+B D to detach, `tmux attach -t ocr` to check progress any time

# once done (or periodically, mid-run - it's just files on disk)
aws s3 sync ~/.share_ocr/csv s3://your-share-ocr-bucket/results
```

`certificates-part-*.csv` and `certificates-failed.csv` land in
`~/.share_ocr/csv` exactly like on Windows - `certificates-failed.csv` is
the same failed-files sheet, `Review` is the same mostly-"No" column.

## 7. Persistent browser service (survives reboots)

`setup_ec2.sh` installs the unit file and creates the Flask session secret.
Configure the security-group rule and SSM instance role/parameters first,
then follow the browser UI instructions at the top of this guide. Uploaded
files can be submitted from the browser without manually copying them into
`/home/ubuntu/scans`.

The queue is a resumable SQLite DB - a reboot or crash mid-run picks back up
exactly where it left off (see the main README's "What makes it scale"
table); `systemctl restart` is always safe.

## 8. Shutting it down

```bash
aws ec2 terminate-instances --instance-ids <id>
```

Nothing here bills you when the instance is stopped/terminated except the
EBS volume and anything left in S3 - clean those up too if this was a
one-off run.
