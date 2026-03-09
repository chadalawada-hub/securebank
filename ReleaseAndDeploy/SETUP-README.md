# SecureBank — Infrastructure as Code & CI/CD

Complete AWS deployment using CloudFormation + GitHub Actions.
No stored AWS keys — all auth uses GitHub OIDC.

---

## Repository Structure

```
your-repo/
├── backend/                    ← FastAPI application
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── migrate.py              ← Run with: python migrate.py
│   └── tests/
├── frontend/                   ← Static HTML/JS
│   └── index.html
├── cloudformation/             ← Infrastructure templates
│   ├── 01-vpc.yaml             Stack 1: VPC, subnets, security groups, VPC endpoints
│   ├── 02-security-db.yaml     Stack 2: KMS, Secrets Manager, RDS PostgreSQL
│   ├── 03-ecr-iam.yaml         Stack 3: ECR repo, IAM roles, GitHub OIDC provider
│   ├── 04-ecs-alb.yaml         Stack 4: ECS cluster, Fargate, ALB, S3
│   └── 05-dns.yaml             Stack 5: Route 53 DNS records
└── .github/
    └── workflows/
        ├── deploy.yml          ← App deploy on push to main
        ├── infra.yml           ← Infrastructure deploy on CFN changes
        ├── pr-checks.yml       ← Tests + lint on every PR
        └── scheduler.yml       ← Auto stop/start dev environment nightly
```

---

## First-Time Setup (Run Once)

### Step 1 — Request an ACM Certificate

```bash
aws acm request-certificate \
  --domain-name "dev.api.yourdomain.com" \
  --subject-alternative-names "dev.yourdomain.com" \
  --validation-method DNS \
  --region us-east-1

# Note the CertificateArn from the output.
# Add the CNAME validation records to your DNS, then wait:
aws acm wait certificate-validated --certificate-arn <ARN>
```

### Step 2 — Bootstrap IAM Role (one-time, before GitHub OIDC is set up)

Before the full GitHub Actions role exists, you need a temporary IAM role
to deploy the CloudFormation stacks manually:

```bash
# Create a temporary admin role for initial stack deployment
aws iam create-role \
  --role-name securebank-bootstrap-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"AWS":"arn:aws:iam::ACCOUNT_ID:root"},
      "Action":"sts:AssumeRole"
    }]
  }'

aws iam attach-role-policy \
  --role-name securebank-bootstrap-role \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
```

### Step 3 — Deploy CloudFormation Stacks Manually (First Time)

```bash
export PROJECT=securebank
export ENV=dev
export REGION=us-east-1
export GITHUB_ORG=your-github-org
export GITHUB_REPO=your-repo-name
export ACM_ARN=arn:aws:acm:us-east-1:ACCOUNT:certificate/XXXX
export HOSTED_ZONE_ID=Z1234567890ABC

# Stack 1: VPC
aws cloudformation deploy \
  --template-file cloudformation/01-vpc.yaml \
  --stack-name ${PROJECT}-${ENV}-vpc \
  --parameter-overrides ProjectName=${PROJECT} Environment=${ENV} \
  --region ${REGION}

# Stack 2: Security & DB (takes ~5 min for RDS)
aws cloudformation deploy \
  --template-file cloudformation/02-security-db.yaml \
  --stack-name ${PROJECT}-${ENV}-security-db \
  --parameter-overrides ProjectName=${PROJECT} Environment=${ENV} \
    DbInstanceClass=db.t3.micro DbMultiAz=false \
  --region ${REGION}

# Stack 3: ECR & IAM (creates the GitHub Actions role)
aws cloudformation deploy \
  --template-file cloudformation/03-ecr-iam.yaml \
  --stack-name ${PROJECT}-${ENV}-ecr-iam \
  --parameter-overrides ProjectName=${PROJECT} Environment=${ENV} \
    GitHubOrg=${GITHUB_ORG} GitHubRepo=${GITHUB_REPO} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${REGION}

# Stack 4: ECS, ALB, S3
aws cloudformation deploy \
  --template-file cloudformation/04-ecs-alb.yaml \
  --stack-name ${PROJECT}-${ENV}-ecs-alb \
  --parameter-overrides ProjectName=${PROJECT} Environment=${ENV} \
    AcmCertificateArn=${ACM_ARN} UseFargateSpot=true \
  --region ${REGION}

# Stack 5: DNS
aws cloudformation deploy \
  --template-file cloudformation/05-dns.yaml \
  --stack-name ${PROJECT}-${ENV}-dns \
  --parameter-overrides ProjectName=${PROJECT} Environment=${ENV} \
    HostedZoneId=${HOSTED_ZONE_ID} \
    ApiDomainName=dev.api.yourdomain.com \
    FrontendDomainName=dev.yourdomain.com \
  --region ${REGION}
```

### Step 4 — Get the GitHub Actions Role ARN

```bash
aws cloudformation list-exports \
  --query "Exports[?Name=='securebank-dev-GitHubActionsRoleArn'].Value" \
  --output text
```

### Step 5 — Add GitHub Secrets

Go to your GitHub repo → **Settings → Secrets → Actions** and add:

| Secret Name             | Value                                             |
|-------------------------|---------------------------------------------------|
| `AWS_ROLE_ARN`          | ARN from Step 4 above                             |
| `AWS_REGION`            | e.g. `us-east-1`                                  |
| `PROJECT_NAME`          | `securebank`                                      |
| `ENVIRONMENT`           | `dev`                                             |
| `ACM_CERTIFICATE_ARN`   | ACM cert ARN from Step 1                          |
| `HOSTED_ZONE_ID`        | Your Route 53 hosted zone ID                      |
| `API_DOMAIN_NAME`       | e.g. `dev.api.yourdomain.com`                     |
| `FRONTEND_DOMAIN_NAME`  | e.g. `dev.yourdomain.com`                         |
| `GITHUB_ORG`            | Your GitHub org or username                       |
| `GITHUB_REPO_NAME`      | Your repository name                              |
| `SLACK_WEBHOOK_URL`     | (Optional) Slack incoming webhook for alerts      |

### Step 6 — Push and Deploy!

```bash
git add .
git commit -m "feat: initial deployment"
git push origin main
```

GitHub Actions will automatically:
1. Run tests and lint
2. Build and push the Docker image to ECR
3. Run database migrations as a one-off ECS task
4. Deploy the new image to ECS Fargate (rolling update)
5. Sync the frontend to S3

---

## CI/CD Pipeline Overview

```
Push to main
     │
     ▼
┌─────────────┐
│  test job   │  pytest + ruff lint (~2 min)
└──────┬──────┘
       │ passes
       ▼
┌─────────────┐
│  build job  │  docker build → ECR push (~3 min, faster with cache)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ migrate job │  one-off ECS task: python migrate.py (~1 min)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ deploy job  │  ECS rolling update + S3 sync (~2 min)
└─────────────┘

Total: ~8 minutes from git push to live
```

---

## Day-to-Day Commands

```bash
# Trigger a manual deploy (already automatic on push)
gh workflow run deploy.yml

# Stop the dev environment (save money)
gh workflow run scheduler.yml -f action=stop

# Start the dev environment
gh workflow run scheduler.yml -f action=start

# Deploy infrastructure changes only
gh workflow run infra.yml -f action=deploy

# Tear down ALL stacks (careful!)
gh workflow run infra.yml -f action=teardown -f environment=dev

# View live logs
aws logs tail /ecs/securebank-dev --follow --format short

# Scale tasks manually
aws ecs update-service \
  --cluster securebank-dev-cluster \
  --service securebank-dev-service \
  --desired-count 0   # or 1 to restart
```

---

## Update a Google OAuth Secret

```bash
# Fill in your real Google credentials after initial deploy
aws secretsmanager update-secret \
  --secret-id securebank/dev/oauth/google \
  --secret-string '{"client_id":"YOUR_REAL_ID","client_secret":"YOUR_REAL_SECRET"}'
```

---

## Monthly Cost Summary (Dev Environment)

| Scenario                    | Cost/month |
|-----------------------------|------------|
| Full dev (always-on)        | ~$65–90    |
| + FARGATE_SPOT              | ~$59–82    |
| + Night/weekend scheduler   | ~$40–55    |
| Free Tier (new account)     | ~$17–30    |

The scheduler workflow stops RDS + Fargate every weeknight at 8 PM ET
and restarts every weekday at 7 AM ET automatically.
