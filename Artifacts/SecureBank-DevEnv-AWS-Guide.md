🏦

**SecureBank**

Dev Environment — AWS Deployment Guide

*Cost-Optimised Setup for 2–3 Developers · Single-AZ · Minimal Infrastructure*

**Item**

**Detail**

Version

1.0 — Dev / Cost-Optimised

Application

SecureBank FastAPI + PostgreSQL

Cloud Provider

Amazon Web Services (AWS)

Environment

Development (2–3 users)

Strategy

Single-AZ, minimal services, Free Tier where possible

Last Updated

March 2026

# **1\. Dev Environment Architecture Overview**

This guide deploys SecureBank on AWS in a lean, cost-optimised configuration for 2–3 developers. The focus is on keeping the monthly bill as low as possible while maintaining a realistic AWS setup that mirrors production patterns.

**Key cost-reduction decisions:** Single-AZ RDS (no standby replica), one small Fargate task, no NAT Gateway (replaced with VPC endpoints), no WAF (use at production stage), shared ALB, and aggressive use of the AWS Free Tier.

## **1.1 Dev vs Production Architecture Comparison**

**Component**

**Production (Prev. Guide)**

**Dev Environment (This Guide)**

**Saving**

RDS Instance

db.t3.medium Multi-AZ

db.t3.micro Single-AZ

**~$79/mo**

Fargate Tasks

2 tasks × 0.5 vCPU

1 task × 0.25 vCPU

**~$17/mo**

NAT Gateway

1 NAT Gateway

VPC Endpoints (S3/ECR)

**~$35/mo**

WAF

Enabled (3 rules)

Disabled

**~$10/mo**

CloudFront

Full distribution

Optional / minimal

**~$9/mo**

Multi-AZ ALB

2 AZ listeners

1 AZ (dev only)

**~$5/mo**

Enhanced Monitoring

RDS + X-Ray

Basic CloudWatch only

**~$5/mo**

💰 **Total Estimated Saving vs Production**

Production estimate: $200–$240/month

Dev environment estimate: ~$45–$65/month (details in Section 9)

That is a saving of approximately $155–$195 per month (~70% reduction).

# **2\. Step 1 — VPC & Network (Cost-Optimised)**

The biggest network cost in the production guide was the NAT Gateway (~$35/mo). For dev, we replace it with VPC Interface Endpoints for ECR and S3, which cost far less and allow Fargate to pull images and reach AWS services without a NAT Gateway.

## **2.1 Create the VPC**

\# bash

\# Create VPC

VPC\_ID=$(aws ec2 create-vpc \\

\--cidr-block 10.0.0.0/16 \\

\--tag-specifications 'ResourceType=vpc,Tags=\[{Key=Name,Value=securebank-dev-vpc}\]' \\

\--query Vpc.VpcId --output text)

echo "VPC: $VPC\_ID"

\# Enable DNS hostnames (required for VPC endpoints)

aws ec2 modify-vpc-attribute --vpc-id $VPC\_ID --enable-dns-hostnames

aws ec2 modify-vpc-attribute --vpc-id $VPC\_ID --enable-dns-support

## **2.2 Create Subnets — 2 AZs (one public, one private)**

For dev we still use 2 AZs — ALB requires at least 2 subnets. But RDS will only be deployed to one AZ (Single-AZ mode).

\# bash

\# Public subnets (ALB lives here)

PUB\_1A=$(aws ec2 create-subnet --vpc-id $VPC\_ID --cidr-block 10.0.1.0/24 \\

\--availability-zone us-east-1a \\

\--tag-specifications 'ResourceType=subnet,Tags=\[{Key=Name,Value=dev-public-1a}\]' \\

\--query Subnet.SubnetId --output text)

PUB\_1B=$(aws ec2 create-subnet --vpc-id $VPC\_ID --cidr-block 10.0.2.0/24 \\

\--availability-zone us-east-1b \\

\--tag-specifications 'ResourceType=subnet,Tags=\[{Key=Name,Value=dev-public-1b}\]' \\

\--query Subnet.SubnetId --output text)

\# Private app subnet (Fargate — single AZ is fine for dev)

PRIV\_APP=$(aws ec2 create-subnet --vpc-id $VPC\_ID --cidr-block 10.0.11.0/24 \\

\--availability-zone us-east-1a \\

\--tag-specifications 'ResourceType=subnet,Tags=\[{Key=Name,Value=dev-app-private-1a}\]' \\

\--query Subnet.SubnetId --output text)

\# Private DB subnet (RDS — 2 subnets needed for subnet group, but single-AZ instance)

PRIV\_DB\_1A=$(aws ec2 create-subnet --vpc-id $VPC\_ID --cidr-block 10.0.21.0/24 \\

\--availability-zone us-east-1a \\

\--tag-specifications 'ResourceType=subnet,Tags=\[{Key=Name,Value=dev-db-private-1a}\]' \\

\--query Subnet.SubnetId --output text)

PRIV\_DB\_1B=$(aws ec2 create-subnet --vpc-id $VPC\_ID --cidr-block 10.0.22.0/24 \\

\--availability-zone us-east-1b \\

\--tag-specifications 'ResourceType=subnet,Tags=\[{Key=Name,Value=dev-db-private-1b}\]' \\

\--query Subnet.SubnetId --output text)

## **2.3 Internet Gateway (for ALB public access)**

\# bash

IGW=$(aws ec2 create-internet-gateway \\

\--query InternetGateway.InternetGatewayId --output text)

aws ec2 attach-internet-gateway --vpc-id $VPC\_ID --internet-gateway-id $IGW

\# Public route table

PUB\_RT=$(aws ec2 create-route-table --vpc-id $VPC\_ID \\

\--query RouteTable.RouteTableId --output text)

aws ec2 create-route --route-table-id $PUB\_RT \\

\--destination-cidr-block 0.0.0.0/0 --gateway-id $IGW

\# Associate public subnets

aws ec2 associate-route-table --subnet-id $PUB\_1A --route-table-id $PUB\_RT

aws ec2 associate-route-table --subnet-id $PUB\_1B --route-table-id $PUB\_RT

## **2.4 VPC Endpoints — Replace NAT Gateway (saves ~$35/month)**

VPC Interface Endpoints let Fargate tasks reach ECR, S3, Secrets Manager, and CloudWatch without routing through a NAT Gateway. Each endpoint costs ~$7.30/month — still far cheaper than one NAT Gateway.

\# bash

\# Create private route table for app/DB subnets

PRIV\_RT=$(aws ec2 create-route-table --vpc-id $VPC\_ID \\

\--query RouteTable.RouteTableId --output text)

aws ec2 associate-route-table --subnet-id $PRIV\_APP --route-table-id $PRIV\_RT

aws ec2 associate-route-table --subnet-id $PRIV\_DB\_1A --route-table-id $PRIV\_RT

aws ec2 associate-route-table --subnet-id $PRIV\_DB\_1B --route-table-id $PRIV\_RT

\# S3 Gateway Endpoint — FREE (no hourly charge)

aws ec2 create-vpc-endpoint \\

\--vpc-id $VPC\_ID --service-name com.amazonaws.us-east-1.s3 \\

\--route-table-ids $PRIV\_RT --vpc-endpoint-type Gateway

\# ECR API Interface Endpoint (~$7.30/mo)

aws ec2 create-vpc-endpoint \\

\--vpc-id $VPC\_ID --service-name com.amazonaws.us-east-1.ecr.api \\

\--vpc-endpoint-type Interface \\

\--subnet-ids $PRIV\_APP --private-dns-enabled

\# ECR Docker Interface Endpoint (~$7.30/mo)

aws ec2 create-vpc-endpoint \\

\--vpc-id $VPC\_ID --service-name com.amazonaws.us-east-1.ecr.dkr \\

\--vpc-endpoint-type Interface \\

\--subnet-ids $PRIV\_APP --private-dns-enabled

\# Secrets Manager Interface Endpoint (~$7.30/mo)

aws ec2 create-vpc-endpoint \\

\--vpc-id $VPC\_ID --service-name com.amazonaws.us-east-1.secretsmanager \\

\--vpc-endpoint-type Interface \\

\--subnet-ids $PRIV\_APP --private-dns-enabled

\# CloudWatch Logs Interface Endpoint (~$7.30/mo)

aws ec2 create-vpc-endpoint \\

\--vpc-id $VPC\_ID --service-name com.amazonaws.us-east-1.logs \\

\--vpc-endpoint-type Interface \\

\--subnet-ids $PRIV\_APP --private-dns-enabled

\# KMS Interface Endpoint (~$7.30/mo)

aws ec2 create-vpc-endpoint \\

\--vpc-id $VPC\_ID --service-name com.amazonaws.us-east-1.kms \\

\--vpc-endpoint-type Interface \\

\--subnet-ids $PRIV\_APP --private-dns-enabled

ℹ **VPC Endpoint Cost vs NAT Gateway**

NAT Gateway: $32.40/mo hourly + $0.045/GB data processed = ~$35+/mo for typical dev traffic

5 Interface Endpoints: 5 × $7.30 = $36.50/mo — comparable at low volume, but...

S3 Gateway Endpoint: FREE — no hourly charge, no per-GB charge

Alternative (ultra-low cost): Put Fargate in a PUBLIC subnet with auto-assigned public IP.

This is fine for dev — Fargate tasks still have no inbound rules, only outbound.

Eliminates ALL endpoint/NAT costs. Add --assign-public-ip ENABLED to the ECS service.

NOT recommended for production (breaks private subnet isolation model).

# **3\. Step 2 — AWS KMS (Same as Production)**

KMS cost is minimal (~$1/month per key + $0.03 per 10,000 API calls) and there is no dev-specific alternative — KMS is already cost-efficient.

\# bash

KMS\_KEY\_ID=$(aws kms create-key \\

\--description "SecureBank Dev PII Encryption Key" \\

\--key-usage ENCRYPT\_DECRYPT \\

\--origin AWS\_KMS \\

\--query KeyMetadata.KeyId --output text)

aws kms create-alias \\

\--alias-name alias/securebank-dev-pii \\

\--target-key-id $KMS\_KEY\_ID

\# Enable automatic annual key rotation

aws kms enable-key-rotation --key-id $KMS\_KEY\_ID

echo "KMS Key ARN:"

aws kms describe-key --key-id $KMS\_KEY\_ID --query KeyMetadata.Arn --output text

# **4\. Step 3 — RDS PostgreSQL (Single-AZ, Small Instance)**

The biggest change from production: Single-AZ deployment with a db.t3.micro instance. This cuts the database cost from ~$105/month down to ~$14–27/month. No automatic failover — acceptable for a dev environment.

## **4.1 Instance Tier Selection for Dev**

**Instance**

**vCPU**

**RAM**

**Single-AZ Cost/mo**

**Good For**

**Free Tier**

db.t3.micro

2

1 GB

**~$14/mo**

Dev with 2–3 users ✓

Yes (750 hrs)

db.t3.small

2

2 GB

~$27/mo

Slightly heavier dev load

No

db.t3.medium

2

4 GB

~$52/mo

Staging / pre-prod

No

db.t3.medium Multi-AZ

2

4 GB

~$105/mo

Production only

No

💰 **Free Tier Opportunity**

If this is a NEW AWS account (within first 12 months), db.t3.micro qualifies for 750 free hours/month.

750 hours = 31.25 days — covers the entire month for a single instance.

This means your RDS cost could be $0/month for the first year. Take advantage of it!

After 12 months, the monthly cost reverts to ~$14/month (db.t3.micro, Single-AZ, 20 GB gp3).

## **4.2 Create the DB Subnet Group**

\# bash

aws rds create-db-subnet-group \\

\--db-subnet-group-name securebank-dev-db-subnets \\

\--db-subnet-group-description "SecureBank Dev DB subnet group" \\

\--subnet-ids $PRIV\_DB\_1A $PRIV\_DB\_1B

## **4.3 Security Group for RDS**

\# bash

DB\_SG=$(aws ec2 create-security-group \\

\--group-name securebank-dev-db-sg \\

\--description "RDS Dev SG" \\

\--vpc-id $VPC\_ID \\

\--query GroupId --output text)

\# Only allow port 5432 from the Fargate app security group

aws ec2 authorize-security-group-ingress \\

\--group-id $DB\_SG \\

\--protocol tcp --port 5432 \\

\--source-group $FARGATE\_SG

\# Also allow from your developer IPs for direct psql access during dev

aws ec2 authorize-security-group-ingress \\

\--group-id $DB\_SG \\

\--protocol tcp --port 5432 \\

\--cidr YOUR\_OFFICE\_IP/32

## **4.4 Store Password in Secrets Manager**

\# bash

aws secretsmanager create-secret \\

\--name securebank-dev/db/password \\

\--description "RDS dev master password" \\

\--secret-string "$(openssl rand -base64 24)"

## **4.5 Launch RDS — Single-AZ, db.t3.micro**

\# bash

DB\_PASS=$(aws secretsmanager get-secret-value \\

\--secret-id securebank-dev/db/password \\

\--query SecretString --output text)

aws rds create-db-instance \\

\--db-instance-identifier securebank-dev-postgres \\

\--db-instance-class db.t3.micro \\

\--engine postgres \\

\--engine-version 16.1 \\

\--master-username bankadmin \\

\--master-user-password "$DB\_PASS" \\

\--db-name securebank \\

\--vpc-security-group-ids $DB\_SG \\

\--db-subnet-group-name securebank-dev-db-subnets \\

\--no-multi-az \\

\--storage-type gp2 \\

\--allocated-storage 20 \\

\--storage-encrypted \\

\--kms-key-id alias/securebank-dev-pii \\

\--backup-retention-period 3 \\

\--no-deletion-protection \\

\--no-publicly-accessible \\

\--tags Key=Environment,Value=dev Key=Project,Value=SecureBank

\# Wait for it to be ready (~5 min)

aws rds wait db-instance-available \\

\--db-instance-identifier securebank-dev-postgres

\# Get the endpoint

RDS\_ENDPOINT=$(aws rds describe-db-instances \\

\--db-instance-identifier securebank-dev-postgres \\

\--query "DBInstances\[0\].Endpoint.Address" --output text)

echo "RDS Endpoint: $RDS\_ENDPOINT"

⚠ **Single-AZ Trade-offs (Dev Acceptable, Not for Production)**

No automatic failover: if the AZ has an outage, the DB goes down until AWS recovers it (usually < 30 min).

No synchronous standby replica: a hardware failure means a few minutes of downtime + potential data loss of the last checkpoint.

Planned maintenance causes brief downtime (a few seconds to ~2 minutes for minor patches).

For dev with 2–3 users: these trade-offs are completely acceptable.

Upgrade path: change --no-multi-az to --multi-az and run aws rds modify-db-instance when ready for production.

# **5\. Step 4 — ECR & Docker Image**

ECR is cost-efficient: the first 500 MB/month is free, and storage beyond that is $0.10/GB. For dev with a small image (~200 MB), this is essentially free.

\# bash

aws ecr create-repository \\

\--repository-name securebank-dev-api \\

\--image-scanning-configuration scanOnPush=true

ECR\_URI=$(aws ecr describe-repositories \\

\--repository-names securebank-dev-api \\

\--query "repositories\[0\].repositoryUri" --output text)

\# Authenticate, build, and push

aws ecr get-login-password --region us-east-1 | \\

docker login --username AWS --password-stdin $ECR\_URI

cd banking-app/backend

docker build -t $ECR\_URI:latest .

docker push $ECR\_URI:latest

\# Store secrets

aws secretsmanager create-secret \\

\--name securebank-dev/app/secret-key \\

\--secret-string "$(openssl rand -hex 64)"

aws secretsmanager create-secret \\

\--name securebank-dev/db/url \\

\--secret-string "postgresql+asyncpg://bankadmin:${DB\_PASS}@${RDS\_ENDPOINT}:5432/securebank"

💰 **ECR Lifecycle Policy — Keep Image Storage Low**

Add a lifecycle policy to auto-delete old images and stay within the free tier:

aws ecr put-lifecycle-policy --repository-name securebank-dev-api --lifecycle-policy-text '{"rules":\[{"rulePriority":1,"description":"Keep only last 5 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}\]}'

# **6\. Step 5 — ECS Fargate (Single Task, Minimum Resources)**

For dev with 2–3 users, one Fargate task with 0.25 vCPU and 512 MB RAM is more than enough. This cuts Fargate costs to approximately $9–11/month.

## **6.1 Create ECS Cluster**

\# bash

aws ecs create-cluster \\

\--cluster-name securebank-dev-cluster \\

\--capacity-providers FARGATE FARGATE\_SPOT

## **6.2 IAM Roles (same as production, simplified)**

\# bash

\# Task execution role

aws iam create-role --role-name securebank-dev-exec-role \\

\--assume-role-policy-document '{"Version":"2012-10-17","Statement":\[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}\]}'

aws iam attach-role-policy --role-name securebank-dev-exec-role \\

\--policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

aws iam attach-role-policy --role-name securebank-dev-exec-role \\

\--policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite

\# Task role (for KMS access)

aws iam create-role --role-name securebank-dev-task-role \\

\--assume-role-policy-document '{"Version":"2012-10-17","Statement":\[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}\]}'

aws iam put-role-policy --role-name securebank-dev-task-role --policy-name KMS \\

\--policy-document '{"Version":"2012-10-17","Statement":\[{"Effect":"Allow","Action":\["kms:GenerateDataKey","kms:Decrypt"\],"Resource":"\*"}\]}'

## **6.3 Task Definition — Minimum Resource Allocation**

\# json

{

"family": "securebank-dev-api",

"networkMode": "awsvpc",

"requiresCompatibilities": \["FARGATE"\],

"cpu": "256",

"memory": "512",

"executionRoleArn": "arn:aws:iam::ACCOUNT\_ID:role/securebank-dev-exec-role",

"taskRoleArn": "arn:aws:iam::ACCOUNT\_ID:role/securebank-dev-task-role",

"containerDefinitions": \[{

"name": "securebank-api",

"image": "ACCOUNT\_ID.dkr.ecr.us-east-1.amazonaws.com/securebank-dev-api:latest",

"portMappings": \[{"containerPort": 8000, "protocol": "tcp"}\],

"essential": true,

"secrets": \[

{"name":"DATABASE\_URL","valueFrom":"arn:aws:...:securebank-dev/db/url"},

{"name":"SECRET\_KEY","valueFrom":"arn:aws:...:securebank-dev/app/secret-key"},

{"name":"KMS\_KEY\_ID","valueFrom":"arn:aws:...:securebank-dev/kms/arn"}

\],

"environment": \[

{"name":"APP\_ENV","value":"development"},

{"name":"AWS\_REGION","value":"us-east-1"},

{"name":"DEBUG","value":"true"}

\],

"logConfiguration": {

"logDriver": "awslogs",

"options": {

"awslogs-group": "/ecs/securebank-dev",

"awslogs-region": "us-east-1",

"awslogs-stream-prefix": "ecs"

}

}

}\]

}

\# bash

aws ecs register-task-definition --cli-input-json file://task-def-dev.json

## **6.4 Create ECS Service — 1 Task, FARGATE\_SPOT Option**

\# bash

\# Option A: Standard Fargate (predictable, ~$9/mo)

aws ecs create-service \\

\--cluster securebank-dev-cluster \\

\--service-name securebank-dev-service \\

\--task-definition securebank-dev-api \\

\--desired-count 1 \\

\--launch-type FARGATE \\

\--network-configuration "awsvpcConfiguration={

subnets=\[$PRIV\_APP\],

securityGroups=\[$FARGATE\_SG\],

assignPublicIp=DISABLED}" \\

\--load-balancers "targetGroupArn=$TG\_ARN,containerName=securebank-api,containerPort=8000"

\# Option B: FARGATE\_SPOT (up to 70% cheaper, ~$3/mo — task may be interrupted)

aws ecs create-service \\

\--cluster securebank-dev-cluster \\

\--service-name securebank-dev-service \\

\--task-definition securebank-dev-api \\

\--desired-count 1 \\

\--capacity-provider-strategy capacityProvider=FARGATE\_SPOT,weight=1 \\

\--network-configuration "awsvpcConfiguration={

subnets=\[$PRIV\_APP\],

securityGroups=\[$FARGATE\_SG\],

assignPublicIp=DISABLED}" \\

\--load-balancers "targetGroupArn=$TG\_ARN,containerName=securebank-api,containerPort=8000"

ℹ **FARGATE\_SPOT for Dev**

FARGATE\_SPOT uses spare AWS capacity at up to 70% discount (~$3/mo vs $9/mo for 0.25 vCPU).

Trade-off: AWS can reclaim Spot capacity with a 2-minute warning, causing brief task interruption.

For a dev environment with 2–3 developers and no SLA requirement, this is perfectly acceptable.

The task automatically restarts within 1–2 minutes when interrupted.

Do NOT use FARGATE\_SPOT in production — use it only in dev/test environments.

# **7\. Step 6 — Application Load Balancer**

The ALB is required to route HTTPS traffic to Fargate. There is no significantly cheaper alternative for HTTPS termination with Fargate — the ALB base cost (~$16/mo) is unavoidable if you want HTTPS. One cost-saving option for dev is to skip CloudFront and point your domain directly at the ALB.

## **7.1 Request Free SSL Certificate (ACM)**

\# bash

\# SSL certificates from ACM are FREE for use with AWS services

CERT\_ARN=$(aws acm request-certificate \\

\--domain-name dev.securebank.com \\

\--validation-method DNS \\

\--query CertificateArn --output text)

\# Add the CNAME records shown in the console to your DNS, then:

aws acm wait certificate-validated --certificate-arn $CERT\_ARN

## **7.2 Create ALB**

\# bash

FARGATE\_SG=$(aws ec2 create-security-group \\

\--group-name securebank-dev-fargate-sg \\

\--description "Fargate Dev SG" --vpc-id $VPC\_ID \\

\--query GroupId --output text)

ALB\_SG=$(aws ec2 create-security-group \\

\--group-name securebank-dev-alb-sg \\

\--description "ALB Dev SG" --vpc-id $VPC\_ID \\

\--query GroupId --output text)

\# ALB SG: allow 80 and 443 from internet

aws ec2 authorize-security-group-ingress --group-id $ALB\_SG \\

\--protocol tcp --port 80 --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress --group-id $ALB\_SG \\

\--protocol tcp --port 443 --cidr 0.0.0.0/0

\# Fargate SG: allow 8000 only from ALB SG

aws ec2 authorize-security-group-ingress --group-id $FARGATE\_SG \\

\--protocol tcp --port 8000 --source-group $ALB\_SG

ALB\_ARN=$(aws elbv2 create-load-balancer \\

\--name securebank-dev-alb \\

\--subnets $PUB\_1A $PUB\_1B \\

\--security-groups $ALB\_SG \\

\--scheme internet-facing --type application \\

\--query "LoadBalancers\[0\].LoadBalancerArn" --output text)

TG\_ARN=$(aws elbv2 create-target-group \\

\--name securebank-dev-tg \\

\--protocol HTTP --port 8000 \\

\--vpc-id $VPC\_ID --target-type ip \\

\--health-check-path /health \\

\--query "TargetGroups\[0\].TargetGroupArn" --output text)

\# HTTP → HTTPS redirect

aws elbv2 create-listener --load-balancer-arn $ALB\_ARN \\

\--protocol HTTP --port 80 \\

\--default-actions Type=redirect,RedirectConfig="{Protocol=HTTPS,Port=443,StatusCode=HTTP\_301}"

\# HTTPS → Fargate

aws elbv2 create-listener --load-balancer-arn $ALB\_ARN \\

\--protocol HTTPS --port 443 \\

\--certificates CertificateArn=$CERT\_ARN \\

\--ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \\

\--default-actions Type=forward,TargetGroupArn=$TG\_ARN

# **8\. Step 7 — Frontend, DNS & Database Migration**

## **8.1 S3 Frontend Hosting (Skip CloudFront for Dev)**

For dev, skip CloudFront and serve the frontend directly from S3 via a simple static website. This removes the CloudFront distribution cost (~$9/mo). Developers can also just open the HTML file locally.

\# bash

\# Create S3 bucket for frontend

BUCKET="securebank-dev-frontend-$(aws sts get-caller-identity --query Account --output text)"

aws s3api create-bucket --bucket $BUCKET --region us-east-1

\# Enable static website hosting (for direct S3 access)

aws s3api put-bucket-website --bucket $BUCKET \\

\--website-configuration '{"IndexDocument":{"Suffix":"index.html"}}'

\# Upload frontend

aws s3 sync banking-app/frontend/ s3://$BUCKET/

\# For dev: update the API URL in index.html to point to the ALB DNS

ALB\_DNS=$(aws elbv2 describe-load-balancers \\

\--load-balancer-arns $ALB\_ARN \\

\--query "LoadBalancers\[0\].DNSName" --output text)

echo "Update API const in index.html to: https://$ALB\_DNS/api/v1"

## **8.2 Route 53 DNS (Point subdomain to ALB)**

\# bash

\# Get your hosted zone ID

ZONE\_ID=$(aws route53 list-hosted-zones \\

\--query "HostedZones\[?Name=='securebank.com.'\].Id" --output text | cut -d/ -f3)

\# Get the ALB hosted zone ID (needed for alias record)

ALB\_ZONE=$(aws elbv2 describe-load-balancers \\

\--load-balancer-arns $ALB\_ARN \\

\--query "LoadBalancers\[0\].CanonicalHostedZoneId" --output text)

\# Create A record for dev subdomain → ALB

aws route53 change-resource-record-sets --hosted-zone-id $ZONE\_ID \\

\--change-batch '{"Changes":\[{"Action":"CREATE","ResourceRecordSet":{

"Name":"dev.securebank.com","Type":"A",

"AliasTarget":{"HostedZoneId":"'$ALB\_ZONE'",

"DNSName":"'$ALB\_DNS'","EvaluateTargetHealth":true}}}\]}'

## **8.3 Run Database Migrations**

\# bash

\# Run the table-creation script against the RDS instance

\# Method 1: Via a one-off ECS task (recommended)

aws ecs run-task \\

\--cluster securebank-dev-cluster \\

\--task-definition securebank-dev-api \\

\--launch-type FARGATE \\

\--network-configuration "awsvpcConfiguration={subnets=\[$PRIV\_APP\],securityGroups=\[$FARGATE\_SG\],assignPublicIp=DISABLED}" \\

\--overrides '{"containerOverrides":\[{"name":"securebank-api","command":\["python","migrate.py"\]}\]}'

\# Method 2: Forward RDS port to your laptop via SSM (if RDS is accessible)

\# aws ssm start-session --target <bastion-id> \\

\# --document-name AWS-StartPortForwardingSessionToRemoteHost \\

\# --parameters host="$RDS\_ENDPOINT",portNumber="5432",localPortNumber="5432"

\# python migrate.py # run locally with DATABASE\_URL=postgresql://...@localhost:5432/securebank

# **9\. Revised Cost Estimation — Dev Environment**

All prices based on us-east-1 (N. Virginia), March 2026. Assumes 2–3 developers, low traffic, business-hours usage pattern (~200 API requests/day).

## **9.1 Detailed Monthly Cost Breakdown**

**AWS Service**

**Configuration**

**Est. Monthly Cost**

**Notes**

RDS PostgreSQL

db.t3.micro, Single-AZ, 20 GB gp2

**$14 – $27**

Free Tier eligible (first 12 mo): $0

ECS Fargate

1 task × 0.25 vCPU / 0.5 GB, 24/7

$9 – $11

Use FARGATE\_SPOT to reduce to ~$3/mo

ALB

1 ALB + minimal LCU (low dev traffic)

$17 – $19

$16.20/mo base is unavoidable for HTTPS

VPC Endpoints

5 Interface + 1 S3 Gateway endpoint

$21 – $24

Or use public subnet Fargate: $0

CloudWatch Logs

5 GB/mo logs, 30-day retention

$2 – $4

First 5 GB/mo free

KMS

1 CMK + ~10K API calls/mo

$1.00 – $1.30

$1/key/mo; first 20K calls free

Secrets Manager

3 secrets

$1.20

$0.40/secret/mo

ECR

<500 MB images (2–3 tags)

$0 – $0.50

Free Tier: 500 MB/mo included

S3 (frontend)

<1 GB storage

$0 – $0.25

Free Tier: 5 GB + 20K GET/mo

Route 53

1 hosted zone

$0.50

$0.50/zone/mo flat

ACM Certificate

Public SSL cert

FREE

Always free for AWS services

Data Transfer

~5 GB/mo (dev traffic)

$0 – $0.45

First 1 GB/mo free, then $0.09/GB

## **9.2 Monthly Total Scenarios**

**Scenario**

**Monthly Estimate**

**What Changes**

Standard (private subnets + VPC endpoints)

**~$65 – $90 / month**

Base setup from this guide

With FARGATE\_SPOT

**~$59 – $82 / month**

Swap Fargate for Spot (~$3 vs $9)

Public subnet Fargate (no endpoints/NAT)

**~$40 – $55 / month**

Eliminate VPC endpoint costs (~$21/mo)

Free Tier account + public subnet Fargate

**~$22 – $35 / month**

RDS free + no VPC endpoints (first year)

Free Tier + FARGATE\_SPOT + public subnet

**~$17 – $25 / month**

All optimisations applied (first year)

💰 **Lowest Possible Dev Cost: ~$17/month**

Use a new AWS account (Free Tier): RDS db.t3.micro = $0 for 12 months

Put Fargate in a PUBLIC subnet (assignPublicIp=ENABLED): eliminates NAT + VPC endpoints

Use FARGATE\_SPOT: ~$3/mo instead of $9/mo

Result: ALB ~$18 + Fargate ~$3 + KMS ~$1 + Secrets ~$1.20 + misc ~$2 = ~$25/mo total

After Free Tier expires: add ~$14/mo for RDS = ~$39/mo total

## **9.3 Cost vs Production — Side-by-Side Summary**

**Category**

**Production Guide**

**Dev Guide (Standard)**

**Dev Guide (Min Cost)**

RDS

$105/mo

$14–$27/mo

$0/mo (Free Tier)

Fargate

$28–$32/mo

$9–$11/mo

~$3/mo (Spot)

NAT / Networking

$35–$40/mo

$21–$24/mo (VPC EP)

$0 (public subnet)

ALB

$18–$22/mo

$17–$19/mo

$17–$19/mo

WAF

$8–$12/mo

Skipped

Skipped

CloudFront

$9–$12/mo

Skipped

Skipped

Other (KMS, logs, etc.)

$10–$15/mo

$5–$7/mo

$4–$6/mo

**TOTAL**

**$200–$240/mo**

**$65–$90/mo**

**$17–$35/mo**

## **9.4 Scheduling: Turn Off RDS at Night**

If developers only work business hours (8 AM–8 PM), you can schedule RDS to stop overnight, saving ~58% of compute hours. Stopped RDS instances do not incur compute charges, only storage charges.

\# bash

\# Stop RDS at night (e.g. 8 PM ET)

aws events put-rule \\

\--name "StopDevRDS" \\

\--schedule-expression "cron(0 0 \* \* ? \*)" \\

\--state ENABLED

\# Start RDS in the morning (e.g. 7 AM ET)

aws events put-rule \\

\--name "StartDevRDS" \\

\--schedule-expression "cron(0 11 \* \* ? \*)" \\

\--state ENABLED

\# NOTE: AWS auto-starts a stopped RDS after 7 days.

\# Use Instance Scheduler (free AWS solution) for reliable scheduling:

\# https://aws.amazon.com/solutions/implementations/instance-scheduler-on-aws/

\# Potential saving: run 13 hrs/day × 30 days = 390 hrs instead of 720 hrs

\# db.t3.micro: $0.017/hr × 330 saved hours = ~$5.60/mo additional saving

# **10\. Day-to-Day Operations for Dev**

## **10.1 Stop/Start the Stack When Not in Use**

\# bash

\# STOP: Scale Fargate to 0 tasks (no compute charge when stopped)

aws ecs update-service \\

\--cluster securebank-dev-cluster \\

\--service securebank-dev-service \\

\--desired-count 0

\# STOP: Stop RDS (saves ~$0.017/hr = ~$12/mo if stopped nights+weekends)

aws rds stop-db-instance \\

\--db-instance-identifier securebank-dev-postgres

\# START: Restart RDS (takes ~2 min to become available)

aws rds start-db-instance \\

\--db-instance-identifier securebank-dev-postgres

aws rds wait db-instance-available \\

\--db-instance-identifier securebank-dev-postgres

\# START: Scale Fargate back to 1 task

aws ecs update-service \\

\--cluster securebank-dev-cluster \\

\--service securebank-dev-service \\

\--desired-count 1

## **10.2 Deploy a New Build**

\# bash

\# Build, push, and force ECS to redeploy

docker build -t $ECR\_URI:latest banking-app/backend/

docker push $ECR\_URI:latest

aws ecs update-service \\

\--cluster securebank-dev-cluster \\

\--service securebank-dev-service \\

\--force-new-deployment

## **10.3 View Live Logs**

\# bash

\# Tail logs from the running Fargate task

aws logs tail /ecs/securebank-dev --follow --format short

## **10.4 Set a Budget Alert (Recommended)**

\# bash

\# Create a $100/month budget with email alert at 80% and 100%

aws budgets create-budget \\

\--account-id $(aws sts get-caller-identity --query Account --output text) \\

\--budget '{"BudgetName":"SecureBank-Dev","BudgetLimit":{"Amount":"100","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}' \\

\--notifications-with-subscribers '\[

{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER\_THAN","Threshold":80},

"Subscribers":\[{"SubscriptionType":"EMAIL","Address":"devteam@company.com"}\]},

{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER\_THAN","Threshold":100},

"Subscribers":\[{"SubscriptionType":"EMAIL","Address":"devteam@company.com"}\]}

\]'

⚠ **Important Reminders**

ALB continues to charge ~$16/mo even when Fargate is scaled to 0. Delete the ALB if the environment will be idle for weeks.

Stopped RDS auto-restarts after 7 days (AWS limitation). Use the Instance Scheduler solution to keep it stopped reliably.

VPC Interface Endpoints charge hourly even when not in use. If using the public subnet option, delete them.

Always check your AWS Cost Explorer weekly: console.aws.amazon.com/cost-management/home

# **11\. Upgrade Path to Production**

When you are ready to go to production, apply these changes one at a time. No re-deployment from scratch is required.

**Step**

**Change**

**Command / Action**

**Cost Impact**

1

Enable Multi-AZ on RDS

aws rds modify-db-instance --db-instance-identifier securebank-dev-postgres --multi-az --apply-immediately

+$14–52/mo

2

Upgrade RDS instance

aws rds modify-db-instance ... --db-instance-class db.t3.medium

+$38/mo

3

Add NAT Gateway + private subnets

aws ec2 create-nat-gateway ... (see production guide)

+$35/mo

4

Scale Fargate to 2+ tasks

aws ecs update-service ... --desired-count 2

+$9/mo per task

5

Enable WAF on ALB

aws wafv2 create-web-acl ... (see production guide)

+$10/mo

6

Add CloudFront CDN

aws cloudfront create-distribution ... (see production guide)

+$9/mo

7

Enable RDS deletion protection

aws rds modify-db-instance ... --deletion-protection

$0

8

Enable GuardDuty

aws guardduty create-detector --enable

+$1–4/mo

*— End of Document —*