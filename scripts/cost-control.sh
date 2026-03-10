# bash
#!/bin/bash
# ═══════════════════════════════════════════════════════
# SecureBank Cost Control Script
# Usage: ./cost-control.sh [stop|start|status|nuke|restore]
# ═══════════════════════════════════════════════════════
set -euo pipefail
 
PROJECT="${PROJECT:-securebank}"
ENV="${ENV:-dev}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
 
# Helper: get a CloudFormation export value
cfn_export() {
  aws cloudformation list-exports --region $REGION \
    --query "Exports[?Name=='${PROJECT}-${ENV}-$1'].Value" \
    --output text
}
 
CLUSTER=$(cfn_export ClusterName)   || CLUSTER=""
SERVICE=$(cfn_export ServiceName)   || SERVICE=""
RDS_ID=$(cfn_export RdsInstanceId)  || RDS_ID=""
ALB_ARN=$(cfn_export AlbArn)        || ALB_ARN=""

# bash
# ─── STATUS ─────────────────────────────────────────────
status() {
  echo "╔══════════════════════════════════════╗"
  echo "║   SecureBank Dev — Resource Status   ║"
  echo "╚══════════════════════════════════════╝"
 
  # Fargate
  if [ -n "$CLUSTER" ] && [ -n "$SERVICE" ]; then
    RUNNING=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE \
      --query "services[0].runningCount" --output text 2>/dev/null || echo "?")
    DESIRED=$(aws ecs describe-services --cluster $CLUSTER --services $SERVICE \
      --query "services[0].desiredCount" --output text 2>/dev/null || echo "?")
    echo "  Fargate:  ${RUNNING}/${DESIRED} tasks running"
  fi
 
  # RDS
  if [ -n "$RDS_ID" ]; then
    RDS_STATUS=$(aws rds describe-db-instances \
      --db-instance-identifier $RDS_ID \
      --query "DBInstances[0].DBInstanceStatus" --output text 2>/dev/null || echo "not found")
    echo "  RDS:      $RDS_STATUS"
  fi
 
  # ALB
  if [ -n "$ALB_ARN" ]; then
    ALB_STATUS=$(aws elbv2 describe-load-balancers \
      --load-balancer-arns $ALB_ARN \
      --query "LoadBalancers[0].State.Code" --output text 2>/dev/null || echo "not found")
    echo "  ALB:      $ALB_STATUS"
  fi
 
  # ECS Task details
  echo ""
  echo "  To view logs: aws logs tail /ecs/${PROJECT}-${ENV} --follow"
}

# bash
# ─── STOP ───────────────────────────────────────────────
# Stops: Fargate (scale to 0) + RDS (stopped state)
# Cost while stopped: ALB ~$0.008/hr, Endpoints ~$0.05/hr, KMS/Secrets <$0.01/day
stop() {
  echo "⏸  Stopping SecureBank dev environment..."
 
  # Scale Fargate to 0 — no vCPU/memory charges while at 0
  echo "  → Scaling Fargate to 0 tasks..."
  aws ecs update-service \
    --cluster $CLUSTER --service $SERVICE \
    --desired-count 0 --region $REGION > /dev/null
  echo "     Fargate: scaled to 0 ✓"
 
  # Stop RDS — saves ~$0.017/hr while stopped (storage still charges ~$0.115/mo)
  RDS_STATUS=$(aws rds describe-db-instances \
    --db-instance-identifier $RDS_ID \
    --query "DBInstances[0].DBInstanceStatus" --output text)
 
  if [ "$RDS_STATUS" = "available" ]; then
    echo "  → Stopping RDS instance..."
    aws rds stop-db-instance \
      --db-instance-identifier $RDS_ID --region $REGION > /dev/null
    echo "     RDS: stopping (takes ~2 min) ✓"
  else
    echo "     RDS: already in state '$RDS_STATUS', skipping"
  fi
 
  echo ""
  echo "✅ Environment stopped."
  echo "   Remaining charges: ALB ~\$0.008/hr + VPC Endpoints ~\$0.05/hr"
  echo "   Run ./cost-control.sh start to resume"
}

# bash
# ─── START ──────────────────────────────────────────────
# Starts: RDS → waits for available → scales Fargate back to 1
start() {
  echo "▶  Starting SecureBank dev environment..."
 
  # Start RDS first (takes longest)
  RDS_STATUS=$(aws rds describe-db-instances \
    --db-instance-identifier $RDS_ID \
    --query "DBInstances[0].DBInstanceStatus" --output text)
 
  if [ "$RDS_STATUS" = "stopped" ]; then
    echo "  → Starting RDS instance..."
    aws rds start-db-instance \
      --db-instance-identifier $RDS_ID --region $REGION > /dev/null
    echo "  → Waiting for RDS to become available (~2-3 min)..."
    aws rds wait db-instance-available \
      --db-instance-identifier $RDS_ID --region $REGION
    echo "     RDS: available ✓"
  else
    echo "     RDS: already '$RDS_STATUS'"
  fi
 
  # Now scale Fargate back up
  echo "  → Scaling Fargate to 1 task..."
  aws ecs update-service \
    --cluster $CLUSTER --service $SERVICE \
    --desired-count 1 --region $REGION > /dev/null
  echo "     Fargate: scaling to 1 task ✓"
 
  echo ""
  echo "✅ Environment started. Fargate task will be healthy in ~60 seconds."
  echo "   Run ./cost-control.sh status to check"
}

# bash
# ─── NUKE (maximum savings — removes expensive resources) ─
# Removes: ALB + VPC Endpoints (saves ~$13/hr vs just stopping)
# Use for multi-day breaks. Run "restore" to bring everything back.
# WARNING: DNS will stop working while nuked.
nuke() {
  echo "🔥 NUKE mode — removing ALB and VPC Endpoints for max savings..."
  read -p "Are you sure? DNS will go down. Type YES to continue: " CONFIRM
  [ "$CONFIRM" != "YES" ] && echo "Cancelled." && exit 0
 
  # First stop Fargate and RDS
  stop
 
  # Delete DNS stack (points to ALB)
  echo "  → Removing DNS records..."
  aws cloudformation delete-stack \
    --stack-name ${PROJECT}-${ENV}-dns --region $REGION
  aws cloudformation wait stack-delete-complete \
    --stack-name ${PROJECT}-${ENV}-dns --region $REGION 2>/dev/null || true
 
  # Delete ECS/ALB stack
  echo "  → Removing ALB and ECS service..."
  aws cloudformation delete-stack \
    --stack-name ${PROJECT}-${ENV}-ecs-alb --region $REGION
  aws cloudformation wait stack-delete-complete \
    --stack-name ${PROJECT}-${ENV}-ecs-alb --region $REGION 2>/dev/null || true
 
  echo "✅ Nuked. Daily cost now: ~\$0.15/day (RDS storage + Secrets + KMS)"
  echo "   Run ./cost-control.sh restore to bring it all back"
}

# bash
# ─── RESTORE (after nuke) ───────────────────────────────
restore() {
  echo "🔄 Restoring SecureBank from nuked state..."
 
  echo "  → Redeploying Stack 4 (ECS + ALB)..."
  aws cloudformation deploy \
    --template-file cloudformation/04-ecs-alb.yaml \
    --stack-name ${PROJECT}-${ENV}-ecs-alb \
    --parameter-overrides \
      ProjectName=${PROJECT} Environment=${ENV} \
      TaskCpu=256 TaskMemory=512 DesiredCount=1 \
      UseFargateSpot=true AcmCertificateArn=${CERT_ARN} \
    --region $REGION --no-fail-on-empty-changeset
 
  echo "  → Redeploying Stack 5 (DNS)..."
  aws cloudformation deploy \
    --template-file cloudformation/05-dns.yaml \
    --stack-name ${PROJECT}-${ENV}-dns \
    --parameter-overrides \
      ProjectName=${PROJECT} Environment=${ENV} \
      HostedZoneId=${HOSTED_ZONE_ID} \
      ApiDomainName=dev.api.yourdomain.com \
      FrontendDomainName=dev.yourdomain.com \
    --region $REGION --no-fail-on-empty-changeset
 
  # Start RDS and Fargate
  start
 
  echo "✅ Restored. Trigger a new deploy to get the latest image:"
  echo "   git commit --allow-empty -m \"restore: redeploy\" && git push"
}

# bash
# ─── ENTRYPOINT ─────────────────────────────────────────
case "${1:-status}" in
  stop)    stop    ;;
  start)   start   ;;
  status)  status  ;;
  nuke)    nuke    ;;
  restore) restore ;;
  *) echo "Usage: $0 [stop|start|status|nuke|restore]"; exit 1 ;;
esac
