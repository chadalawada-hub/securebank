#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# SecureBank — Cost Control Script
# ───────────────────────────────────────────────────────────────────────────
# Stops and starts every billable AWS resource in the dev environment.
# Save this as: scripts/cost-control.sh
# Make executable: chmod +x scripts/cost-control.sh
#
# Usage:
#   ./cost-control.sh status   — Show current state and cost of all resources
#   ./cost-control.sh stop     — Stop Fargate + RDS (saves ~$0.026/hr)
#   ./cost-control.sh start    — Start RDS then Fargate (RDS takes ~2-3 min)
#   ./cost-control.sh nuke     — Remove ALB + ECS stack for maximum savings
#   ./cost-control.sh restore  — Rebuild ALB + ECS stack after nuke
#
# Cost Comparison:
#   Fully running:  ~$2.10/day  ($0.087/hr)
#   Stopped:        ~$1.39/day  ($0.058/hr) — ALB + VPC Endpoints still charge
#   Nuked:          ~$0.15/day  ($0.006/hr) — Only RDS storage + KMS + Secrets
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────
PROJECT="${PROJECT:-securebank}"
ENV="${ENV:-dev}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
CERT_ARN="${CERT_ARN:-}"
HOSTED_ZONE_ID="${HOSTED_ZONE_ID:-}"
API_DOMAIN="${API_DOMAIN:-dev.api.yourdomain.com}"
FRONTEND_DOMAIN="${FRONTEND_DOMAIN:-dev.yourdomain.com}"

# ── Colours ───────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ── Helper: get a CloudFormation export value ──────────────────────────────
cfn_export() {
  aws cloudformation list-exports \
    --region "$REGION" \
    --query "Exports[?Name=='${PROJECT}-${ENV}-$1'].Value" \
    --output text 2>/dev/null || echo ""
}

# ── Helper: print a coloured status line ──────────────────────────────────
status_line() {
  local label="$1" value="$2" color="$3"
  printf "  %-18s ${color}%s${RESET}\n" "${label}:" "${value}"
}

# ════════════════════════════════════════════════════════════════════════════
# STATUS — show the current state of every billable resource
# ════════════════════════════════════════════════════════════════════════════
status() {
  echo ""
  echo -e "${BOLD}${BLUE}╔══════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}${BLUE}║   SecureBank Dev — Resource Status           ║${RESET}"
  echo -e "${BOLD}${BLUE}╚══════════════════════════════════════════════╝${RESET}"
  echo ""

  local CLUSTER SERVICE RDS_ID ALB_ARN

  CLUSTER=$(cfn_export ClusterName)
  SERVICE=$(cfn_export ServiceName)
  RDS_ID=$(cfn_export RdsInstanceId)
  ALB_ARN=$(cfn_export AlbArn)

  # ECS / Fargate
  if [ -n "$CLUSTER" ] && [ -n "$SERVICE" ]; then
    local RUNNING DESIRED
    RUNNING=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
      --region "$REGION" --query "services[0].runningCount" --output text 2>/dev/null || echo "?")
    DESIRED=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
      --region "$REGION" --query "services[0].desiredCount" --output text 2>/dev/null || echo "?")
    if [ "$RUNNING" = "0" ]; then
      status_line "Fargate" "${RUNNING}/${DESIRED} tasks — STOPPED" "$YELLOW"
    else
      status_line "Fargate" "${RUNNING}/${DESIRED} tasks — RUNNING" "$GREEN"
    fi
  else
    status_line "Fargate" "Stack not deployed" "$YELLOW"
  fi

  # RDS
  if [ -n "$RDS_ID" ]; then
    local RDS_STATUS
    RDS_STATUS=$(aws rds describe-db-instances \
      --db-instance-identifier "$RDS_ID" \
      --region "$REGION" \
      --query "DBInstances[0].DBInstanceStatus" --output text 2>/dev/null || echo "not found")
    case "$RDS_STATUS" in
      available) status_line "RDS" "$RDS_STATUS" "$GREEN" ;;
      stopped)   status_line "RDS" "$RDS_STATUS" "$YELLOW" ;;
      *)         status_line "RDS" "$RDS_STATUS" "$CYAN" ;;
    esac
  else
    status_line "RDS" "Stack not deployed" "$YELLOW"
  fi

  # ALB
  if [ -n "$ALB_ARN" ]; then
    local ALB_STATE
    ALB_STATE=$(aws elbv2 describe-load-balancers \
      --load-balancer-arns "$ALB_ARN" \
      --region "$REGION" \
      --query "LoadBalancers[0].State.Code" --output text 2>/dev/null || echo "not found")
    if [ "$ALB_STATE" = "active" ]; then
      status_line "ALB" "$ALB_STATE (charging ~\$0.008/hr)" "$GREEN"
    else
      status_line "ALB" "$ALB_STATE" "$YELLOW"
    fi
  else
    status_line "ALB" "Not deployed (nuked or not yet created)" "$YELLOW"
  fi

  # VPC Endpoints check
  local VPCE_COUNT
  VPCE_COUNT=$(aws ec2 describe-vpc-endpoints \
    --region "$REGION" \
    --filters "Name=tag:Project,Values=${PROJECT}" "Name=state,Values=available" \
    --query "length(VpcEndpoints)" --output text 2>/dev/null || echo "0")
  if [ "$VPCE_COUNT" -gt 0 ] 2>/dev/null; then
    status_line "VPC Endpoints" "${VPCE_COUNT} active (~\$0.01/hr each)" "$CYAN"
  fi

  # KMS / Secrets
  local KMS_KEY_ID
  KMS_KEY_ID=$(cfn_export KmsKeyId)
  if [ -n "$KMS_KEY_ID" ]; then
    status_line "KMS + Secrets" "Active (~\$1.50/mo flat)" "$GREEN"
  fi

  echo ""

  # Current estimate
  local TASK_COUNT="${RUNNING:-0}"
  if [ "$TASK_COUNT" = "?" ]; then TASK_COUNT=0; fi

  echo -e "  ${BOLD}Approximate current cost:${RESET}"
  if [ -n "$ALB_ARN" ]; then
    echo -e "  ${CYAN}  ALB + Fargate + RDS: running = ~\$2.10/day | stopped = ~\$1.39/day${RESET}"
  else
    echo -e "  ${GREEN}  ALB removed (nuked): ~\$0.15/day${RESET}"
  fi
  echo ""
  echo -e "  ${BOLD}Quick actions:${RESET}"
  echo "    $0 stop     — Stop Fargate + RDS (saves ~\$0.63/day)"
  echo "    $0 start    — Start everything"
  echo "    $0 nuke     — Remove ALB for max savings (~\$0.15/day)"
  echo ""
}

# ════════════════════════════════════════════════════════════════════════════
# STOP — Scale Fargate to 0 and stop RDS
# ════════════════════════════════════════════════════════════════════════════
stop() {
  echo ""
  echo -e "${BOLD}${YELLOW}⏸  Stopping SecureBank dev environment...${RESET}"
  echo ""

  local CLUSTER SERVICE RDS_ID
  CLUSTER=$(cfn_export ClusterName)
  SERVICE=$(cfn_export ServiceName)
  RDS_ID=$(cfn_export RdsInstanceId)

  if [ -z "$CLUSTER" ] || [ -z "$SERVICE" ]; then
    echo -e "${RED}  ✗ Could not find ECS cluster/service. Is Stack 4 deployed?${RESET}"
  else
    echo -e "  → Scaling Fargate to 0 tasks..."
    aws ecs update-service \
      --cluster "$CLUSTER" --service "$SERVICE" \
      --desired-count 0 --region "$REGION" > /dev/null
    echo -e "  ${GREEN}✓ Fargate: scaled to 0 (no vCPU/memory charges)${RESET}"
  fi

  if [ -z "$RDS_ID" ]; then
    echo -e "${RED}  ✗ Could not find RDS instance. Is Stack 2 deployed?${RESET}"
  else
    local RDS_STATUS
    RDS_STATUS=$(aws rds describe-db-instances \
      --db-instance-identifier "$RDS_ID" \
      --region "$REGION" \
      --query "DBInstances[0].DBInstanceStatus" --output text)

    if [ "$RDS_STATUS" = "available" ]; then
      echo -e "  → Stopping RDS instance (saves ~\$0.017/hr)..."
      aws rds stop-db-instance \
        --db-instance-identifier "$RDS_ID" --region "$REGION" > /dev/null
      echo -e "  ${GREEN}✓ RDS: stopping (will be fully stopped in ~2 minutes)${RESET}"
      echo -e "  ${CYAN}  Note: RDS storage still charges ~\$0.115/mo while stopped${RESET}"
    elif [ "$RDS_STATUS" = "stopped" ]; then
      echo -e "  ${YELLOW}  RDS: already stopped${RESET}"
    else
      echo -e "  ${YELLOW}  RDS: in state '${RDS_STATUS}', skipping${RESET}"
    fi
  fi

  echo ""
  echo -e "${GREEN}✅ Stop complete.${RESET}"
  echo -e "   ${CYAN}Remaining charges: ALB ~\$0.008/hr + VPC Endpoints ~\$0.05/hr${RESET}"
  echo -e "   Run: ${BOLD}$0 start${RESET} to resume"
  echo ""
}

# ════════════════════════════════════════════════════════════════════════════
# START — Start RDS then scale Fargate back to 1
# ════════════════════════════════════════════════════════════════════════════
start() {
  echo ""
  echo -e "${BOLD}${GREEN}▶  Starting SecureBank dev environment...${RESET}"
  echo ""

  local CLUSTER SERVICE RDS_ID
  CLUSTER=$(cfn_export ClusterName)
  SERVICE=$(cfn_export ServiceName)
  RDS_ID=$(cfn_export RdsInstanceId)

  # Start RDS first (takes longest)
  if [ -n "$RDS_ID" ]; then
    local RDS_STATUS
    RDS_STATUS=$(aws rds describe-db-instances \
      --db-instance-identifier "$RDS_ID" \
      --region "$REGION" \
      --query "DBInstances[0].DBInstanceStatus" --output text)

    if [ "$RDS_STATUS" = "stopped" ]; then
      echo -e "  → Starting RDS instance..."
      aws rds start-db-instance \
        --db-instance-identifier "$RDS_ID" --region "$REGION" > /dev/null
      echo -e "  → Waiting for RDS to become available (~2-3 minutes)..."
      aws rds wait db-instance-available \
        --db-instance-identifier "$RDS_ID" --region "$REGION"
      echo -e "  ${GREEN}✓ RDS: available${RESET}"
    elif [ "$RDS_STATUS" = "available" ]; then
      echo -e "  ${GREEN}✓ RDS: already available${RESET}"
    else
      echo -e "  ${YELLOW}  RDS: in state '${RDS_STATUS}', waiting..."
      aws rds wait db-instance-available \
        --db-instance-identifier "$RDS_ID" --region "$REGION" || true
    fi
  fi

  # Scale Fargate back up
  if [ -n "$CLUSTER" ] && [ -n "$SERVICE" ]; then
    echo -e "  → Scaling Fargate to 1 task..."
    aws ecs update-service \
      --cluster "$CLUSTER" --service "$SERVICE" \
      --desired-count 1 --region "$REGION" > /dev/null
    echo -e "  ${GREEN}✓ Fargate: scaling to 1 task (will be ready in ~60 seconds)${RESET}"
  else
    echo -e "${RED}  ✗ ECS service not found. If you ran nuke, run: $0 restore${RESET}"
  fi

  echo ""
  echo -e "${GREEN}✅ Start complete.${RESET}"
  echo -e "   Check status in 60 seconds: ${BOLD}$0 status${RESET}"
  echo ""
}

# ════════════════════════════════════════════════════════════════════════════
# NUKE — Remove ALB + ECS stack for maximum cost savings
# Use for multi-day breaks (holidays, sprints, etc.)
# ════════════════════════════════════════════════════════════════════════════
nuke() {
  echo ""
  echo -e "${BOLD}${RED}🔥 NUKE MODE — Removing ALB and ECS for maximum savings${RESET}"
  echo -e "${RED}   WARNING: Your API and frontend will go offline.${RESET}"
  echo -e "${RED}   The domain will stop resolving until you run restore.${RESET}"
  echo -e "${CYAN}   After nuke, daily cost: ~\$0.15/day (RDS storage + KMS + Secrets)${RESET}"
  echo ""
  read -p "Type YES to confirm: " CONFIRM
  [ "$CONFIRM" != "YES" ] && echo "Cancelled." && exit 0

  # First stop Fargate and RDS to prevent charges during teardown
  stop

  # Delete DNS stack first (it references the ALB)
  echo ""
  echo -e "  → Removing DNS records (Stack 5)..."
  aws cloudformation delete-stack \
    --stack-name "${PROJECT}-${ENV}-dns" --region "$REGION" 2>/dev/null || true
  aws cloudformation wait stack-delete-complete \
    --stack-name "${PROJECT}-${ENV}-dns" --region "$REGION" 2>/dev/null || true
  echo -e "  ${GREEN}✓ DNS stack removed${RESET}"

  # Delete ECS + ALB stack
  echo -e "  → Removing ECS, ALB, and S3 (Stack 4)..."
  aws cloudformation delete-stack \
    --stack-name "${PROJECT}-${ENV}-ecs-alb" --region "$REGION" 2>/dev/null || true
  aws cloudformation wait stack-delete-complete \
    --stack-name "${PROJECT}-${ENV}-ecs-alb" --region "$REGION" 2>/dev/null || true
  echo -e "  ${GREEN}✓ ECS + ALB stack removed${RESET}"

  echo ""
  echo -e "${GREEN}✅ Nuke complete.${RESET}"
  echo -e "   ${CYAN}Remaining: RDS stopped + KMS + Secrets Manager${RESET}"
  echo -e "   ${CYAN}Cost: ~\$0.15/day (\$4.50/month)${RESET}"
  echo -e "   Run: ${BOLD}$0 restore${RESET} to bring everything back"
  echo ""
}

# ════════════════════════════════════════════════════════════════════════════
# RESTORE — Rebuild stacks after nuke
# ════════════════════════════════════════════════════════════════════════════
restore() {
  echo ""
  echo -e "${BOLD}${CYAN}🔄 Restoring SecureBank from nuked state...${RESET}"
  echo ""

  # Validate required variables
  if [ -z "$CERT_ARN" ]; then
    echo -e "${RED}Error: CERT_ARN is not set. Export it first:${RESET}"
    echo "  export CERT_ARN=arn:aws:acm:us-east-1:ACCOUNT:certificate/XXXX"
    exit 1
  fi
  if [ -z "$HOSTED_ZONE_ID" ]; then
    echo -e "${RED}Error: HOSTED_ZONE_ID is not set. Export it first:${RESET}"
    echo "  export HOSTED_ZONE_ID=Z1234567890ABCDEFGH"
    exit 1
  fi

  # Find the cloudformation directory
  CFN_DIR="$(dirname "$0")/../cloudformation"
  if [ ! -f "$CFN_DIR/04-ecs-alb.yaml" ]; then
    CFN_DIR="./cloudformation"
  fi
  if [ ! -f "$CFN_DIR/04-ecs-alb.yaml" ]; then
    echo -e "${RED}Error: Cannot find cloudformation/ directory.${RESET}"
    echo "  Run this script from your repository root."
    exit 1
  fi

  echo -e "  → Redeploying Stack 4 (ECS + ALB)..."
  aws cloudformation deploy \
    --template-file "$CFN_DIR/04-ecs-alb.yaml" \
    --stack-name "${PROJECT}-${ENV}-ecs-alb" \
    --parameter-overrides \
      ProjectName="${PROJECT}" Environment="${ENV}" \
      TaskCpu=256 TaskMemory=512 DesiredCount=1 \
      UseFargateSpot=true \
      AcmCertificateArn="${CERT_ARN}" \
      LogRetentionDays=14 \
    --region "$REGION" \
    --no-fail-on-empty-changeset
  echo -e "  ${GREEN}✓ Stack 4 deployed${RESET}"

  echo -e "  → Redeploying Stack 5 (DNS)..."
  aws cloudformation deploy \
    --template-file "$CFN_DIR/05-dns.yaml" \
    --stack-name "${PROJECT}-${ENV}-dns" \
    --parameter-overrides \
      ProjectName="${PROJECT}" Environment="${ENV}" \
      HostedZoneId="${HOSTED_ZONE_ID}" \
      ApiDomainName="${API_DOMAIN}" \
      FrontendDomainName="${FRONTEND_DOMAIN}" \
    --region "$REGION" \
    --no-fail-on-empty-changeset
  echo -e "  ${GREEN}✓ Stack 5 deployed${RESET}"

  # Start RDS and Fargate
  start

  echo -e "${GREEN}✅ Restore complete.${RESET}"
  echo -e "   Trigger a redeploy to get the latest image:"
  echo -e "   ${BOLD}git commit --allow-empty -m 'restore: redeploy' && git push${RESET}"
  echo ""
}

# ════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ════════════════════════════════════════════════════════════════════════════
case "${1:-status}" in
  stop)    stop    ;;
  start)   start   ;;
  status)  status  ;;
  nuke)    nuke    ;;
  restore) restore ;;
  *)
    echo ""
    echo "Usage: $0 [stop|start|status|nuke|restore]"
    echo ""
    echo "  status   Show current state of all AWS resources"
    echo "  stop     Scale Fargate to 0 + stop RDS  (saves ~\$0.63/day)"
    echo "  start    Start RDS + scale Fargate to 1"
    echo "  nuke     Remove ALB + ECS stack          (max savings: ~\$0.15/day)"
    echo "  restore  Rebuild ALB + ECS after nuke"
    echo ""
    exit 1
    ;;
esac
